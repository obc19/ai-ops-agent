"""K8s 原生采集：不依赖 Prometheus / ELK，直接解析 K8s API 的返回。

这里全部用「真实 API 返回结构的裁剪版」当样本，覆盖裸集群最典型的四种故障：
CrashLoopBackOff、OOMKilled、节点 NotReady、副本缺口；以及 metrics-server 缺失时的降级。
"""

from __future__ import annotations

import pytest

from app.collectors.k8s_adapter import K8sCollector, parse_quantity
from app.config.settings import Settings
from app.models.events import K8sEvent, MetricEvent


def _collector(**overrides) -> K8sCollector:
    settings = Settings(mock_mode=False, kubernetes_api_url="https://k8s.test:6443", **overrides)
    return K8sCollector(settings)


# ---------------- 数量解析 ----------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("250m", 0.25),
        ("2", 2.0),
        ("512Mi", 512 * 1024**2),
        ("1Gi", 1024**3),
        ("100n", 100e-9),
        ("3k", 3000.0),
        ("", 0.0),
        (None, 0.0),
        ("bad", 0.0),
    ],
)
def test_parse_quantity(raw, expected):
    assert parse_quantity(raw) == pytest.approx(expected)


# ---------------- Events ----------------


def test_parse_events_keeps_reason_and_count():
    payload = {
        "items": [
            {
                "metadata": {"namespace": "prod"},
                "involvedObject": {"name": "api-7d9f", "kind": "Pod"},
                "reason": "FailedScheduling",
                "message": "0/3 nodes are available: 3 Insufficient cpu",
                "count": 7,
            }
        ]
    }
    events = _collector()._parse_events(payload)
    assert len(events) == 1
    assert isinstance(events[0], K8sEvent)
    assert events[0].reason == "FailedScheduling"
    assert events[0].count == 7


def test_namespace_filter():
    payload = {
        "items": [
            {
                "metadata": {"namespace": "other"},
                "involvedObject": {"name": "x", "kind": "Pod"},
                "reason": "OOMKilled",
                "message": "",
                "count": 1,
            }
        ]
    }
    # 不配 namespaces = 全收
    assert len(_collector()._parse_events(payload)) == 1
    # 配了 prod 就只收 prod
    assert _collector(kubernetes_namespaces="prod,web")._parse_events(payload) == []


# ---------------- Pods ----------------


def _pod_payload(items):
    return {"items": items}


def test_pod_crashloop_and_restarts():
    payload = _pod_payload(
        [
            {
                "metadata": {"name": "order-1", "namespace": "prod"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": False,
                            "restartCount": 6,
                            "state": {
                                "waiting": {
                                    "reason": "CrashLoopBackOff",
                                    "message": "back-off 5m0s restarting failed container",
                                }
                            },
                        }
                    ],
                },
            }
        ]
    )
    events, index = _collector()._parse_pods(payload)
    reasons = {e.reason for e in events if isinstance(e, K8sEvent)}
    metrics = {e.metric: e.value for e in events if isinstance(e, MetricEvent)}

    assert reasons == {"CrashLoopBackOff"}
    assert metrics["k8s_pod_restarts"] == 6
    assert metrics["k8s_pod_ready"] == 0
    assert "prod/order-1" in index


def test_pod_oomkilled_from_last_state():
    payload = _pod_payload(
        [
            {
                "metadata": {"name": "pay-1", "namespace": "prod"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": True,
                            "restartCount": 1,
                            "state": {"running": {"startedAt": "2026-10-30T01:00:00Z"}},
                            "lastState": {
                                "terminated": {
                                    "reason": "OOMKilled",
                                    "exitCode": 137,
                                    "message": "container exceeded memory limit",
                                }
                            },
                        }
                    ],
                },
            }
        ]
    )
    events, _ = _collector()._parse_pods(payload)
    k8s_events = [e for e in events if isinstance(e, K8sEvent)]
    assert [e.reason for e in k8s_events] == ["OOMKilled"]
    # 次数用 restartCount，不能拿 exitCode(137) 当次数
    assert k8s_events[0].count == 1
    # 已 ready 的 Pod 不该报 not-ready 指标
    assert all(getattr(e, "metric", "") != "k8s_pod_ready" for e in events)


def test_pod_healthy_produces_nothing():
    payload = _pod_payload(
        [
            {
                "metadata": {"name": "web-1", "namespace": "prod"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": "app", "ready": True, "restartCount": 0, "state": {"running": {}}}
                    ],
                },
            }
        ]
    )
    events, _ = _collector()._parse_pods(payload)
    assert events == []


def test_pod_terminating_is_skipped():
    """滚动更新时旧 Pod 会短暂 not ready，不该报警。"""
    payload = _pod_payload(
        [
            {
                "metadata": {
                    "name": "web-old",
                    "namespace": "prod",
                    "deletionTimestamp": "2026-10-30T01:00:00Z",
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": "app", "ready": False, "restartCount": 0, "state": {"running": {}}}
                    ],
                },
            }
        ]
    )
    events, _ = _collector()._parse_pods(payload)
    assert events == []


def test_pod_pending_unschedulable():
    payload = _pod_payload(
        [
            {
                "metadata": {"name": "worker-1", "namespace": "prod"},
                "status": {
                    "phase": "Pending",
                    "conditions": [
                        {
                            "type": "PodScheduled",
                            "status": "False",
                            "reason": "Unschedulable",
                            "message": "0/3 nodes are available: 3 Insufficient memory",
                        }
                    ],
                },
            }
        ]
    )
    events, _ = _collector()._parse_pods(payload)
    k8s_events = [e for e in events if isinstance(e, K8sEvent)]
    assert [e.reason for e in k8s_events] == ["Unschedulable"]
    metrics = {e.metric for e in events if isinstance(e, MetricEvent)}
    assert "k8s_pod_ready" in metrics


# ---------------- Nodes ----------------


def test_node_not_ready_and_pressure():
    payload = {
        "items": [
            {
                "metadata": {"name": "node-1"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "False"},
                        {"type": "MemoryPressure", "status": "True"},
                        {"type": "DiskPressure", "status": "False"},
                    ]
                },
            }
        ]
    }
    events = _collector()._parse_nodes(payload)
    metrics = {e.metric: e.value for e in events if isinstance(e, MetricEvent)}
    assert metrics["k8s_node_ready"] == 0
    assert metrics["k8s_node_memory_pressure"] == 1
    assert "k8s_node_disk_pressure" not in metrics
    reasons = [e.reason for e in events if isinstance(e, K8sEvent)]
    assert reasons == ["NodeNotReady"]


def test_node_healthy_no_events():
    payload = {
        "items": [
            {
                "metadata": {"name": "node-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        ]
    }
    events = _collector()._parse_nodes(payload)
    assert len(events) == 1
    assert isinstance(events[0], MetricEvent)
    assert events[0].value == 1.0


# ---------------- Deployments ----------------


def test_deployment_ready_gap():
    payload = {
        "items": [
            {
                "metadata": {"name": "api", "namespace": "prod"},
                "spec": {"replicas": 4},
                "status": {"readyReplicas": 1},
            },
            {
                "metadata": {"name": "ok", "namespace": "prod"},
                "spec": {"replicas": 2},
                "status": {"readyReplicas": 2},
            },
        ]
    }
    events = _collector()._parse_deployments(payload)
    assert len(events) == 1
    assert events[0].target == "api"
    assert events[0].value == 3.0


# ---------------- metrics-server ----------------


def test_pod_metrics_need_limits():
    index = {
        "prod/api-1": {
            "spec": {"containers": [{"resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]}
        },
        # 没设 limit 的容器算不出占比，应当跳过而不是报个假百分比
        "prod/no-limit": {"spec": {"containers": [{"resources": {}}]}},
    }
    payload = {
        "items": [
            {
                "metadata": {"name": "api-1", "namespace": "prod"},
                "containers": [{"usage": {"cpu": "900m", "memory": "256Mi"}}],
            },
            {
                "metadata": {"name": "no-limit", "namespace": "prod"},
                "containers": [{"usage": {"cpu": "100m", "memory": "64Mi"}}],
            },
        ]
    }
    events = _collector()._parse_pod_metrics(payload, index)
    values = {e.metric: e.value for e in events}
    assert values == {"k8s_pod_cpu_percent": 90.0, "k8s_pod_memory_percent": 50.0}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResponse) -> None:
        self.resp = resp
        self.calls = 0

    async def get(self, path, **kwargs):
        self.calls += 1
        return self.resp


@pytest.mark.asyncio
async def test_metrics_missing_degrades_silently():
    collector = _collector()
    client = _FakeClient(_FakeResponse(404))
    assert await collector._get_metrics(client, "/apis/metrics.k8s.io/v1beta1/pods", {}) == {}
    # 记下「不可用」后不再重复请求，避免每轮刷日志和空转
    assert await collector._get_metrics(client, "/apis/metrics.k8s.io/v1beta1/pods", {}) == {}
    assert client.calls == 1
    assert collector._metrics_available is False


@pytest.mark.asyncio
async def test_metrics_available_marks_true():
    collector = _collector()
    client = _FakeClient(_FakeResponse(200, {"items": []}))
    await collector._get_metrics(client, "/apis/metrics.k8s.io/v1beta1/pods", {})
    assert collector._metrics_available is True


@pytest.mark.asyncio
async def test_rbac_denied_on_one_resource_does_not_break_others():
    """RBAC 逐资源授权，某个 list 被拒不该让整轮采集失败。"""
    collector = _collector()
    client = _FakeClient(_FakeResponse(403))
    assert await collector._get(client, "/api/v1/nodes", {}) == {}


@pytest.mark.asyncio
async def test_snapshot_cache_avoids_repeated_scrape(monkeypatch):
    """一轮检测里会被采两轮，不能每次都 list pods 打 apiserver。"""
    collector = _collector()
    calls = 0

    async def fake_real(window):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(collector, "_real", fake_real)
    K8sCollector.clear_cache()

    await collector.query(None)
    await collector.query(None)
    assert calls == 1

    await collector.query(None, force=True)
    assert calls == 2
    K8sCollector.clear_cache()
