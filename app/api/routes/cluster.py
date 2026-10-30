"""集群实时状态：回答「没装 Prometheus / ELK，你在 K8s 里到底看得到什么」。

数据全部来自 K8s 原生 API（events / pods / nodes / deployments），
metrics-server 存在时再补 CPU/内存用量；不存在就明说「没装」，不装作有。

与 /host 一样刻意不写历史：控制台刷新频率不可控，混进检测基线会污染采样。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_auth
from app.collectors.k8s_adapter import K8sCollector
from app.models.events import K8sEvent, MetricEvent

router = APIRouter(tags=["cluster"])


@router.get("/cluster", summary="K8s 集群实时状态")
async def cluster_status(actor: str = Depends(require_auth)) -> dict:
    collector = K8sCollector()
    base = {
        "available": False,
        "reason": "",
        "in_cluster": collector.in_cluster,
        "metrics_server": None,
        "nodes": [],
        "workloads": [],
        "pod_issues": [],
        "event_count": 0,
    }
    if not collector.enabled:
        base["reason"] = (
            "K8s 采集未启用：集群内运行（有 ServiceAccount）会自动开启，"
            "集群外需配置 sources.kubernetes_api_url / kubernetes_token"
        )
        return base
    if not collector.configured and not collector.settings.mock_mode:
        base["reason"] = (
            "未配置 Kubernetes API 地址：集群外运行需设置 "
            "sources.kubernetes_api_url / kubernetes_token"
        )
        return base

    events = await collector.query(None)

    nodes: dict[str, dict] = {}
    workloads: dict[str, dict] = {}
    pod_issues: dict[str, dict] = {}
    pod_events: list[dict] = []

    for event in events:
        if isinstance(event, MetricEvent):
            if event.metric.startswith("k8s_node_"):
                row = nodes.setdefault(
                    event.target,
                    {"name": event.target, "ready": None, "issues": []},
                )
                if event.metric == "k8s_node_ready":
                    row["ready"] = event.value == 1.0
                else:
                    row["issues"].append(event.metric.removeprefix("k8s_node_").replace("_", " "))
            elif event.metric == "k8s_deployment_ready_gap":
                workloads[event.target] = {
                    "namespace": event.namespace,
                    "name": event.target,
                    "desired": event.labels.get("desired", "?"),
                    "ready": event.labels.get("ready", "?"),
                    "gap": event.value,
                }
            elif event.metric in ("k8s_pod_restarts", "k8s_pod_ready"):
                row = pod_issues.setdefault(
                    event.target,
                    {"namespace": event.namespace, "name": event.target, "restarts": 0, "reasons": []},
                )
                if event.metric == "k8s_pod_restarts":
                    row["restarts"] = int(event.value or 0)
                else:
                    row["not_ready"] = True
        elif isinstance(event, K8sEvent):
            if event.kind == "Pod" and event.namespace:
                row = pod_issues.setdefault(
                    event.target,
                    {"namespace": event.namespace, "name": event.target, "restarts": 0, "reasons": []},
                )
                if event.reason and event.reason not in row["reasons"]:
                    row["reasons"].append(event.reason)
            pod_events.append(
                {
                    "namespace": event.namespace,
                    "target": event.target,
                    "reason": event.reason,
                    "kind": event.kind,
                    "message": (event.message or "")[:200],
                    "count": event.count,
                }
            )

    return {
        **base,
        "available": True,
        "metrics_server": collector._metrics_available,
        "metrics_note": collector._metrics_note,
        "nodes": list(nodes.values()),
        "workloads": list(workloads.values()),
        # 有症状的 Pod 排前面：重启多的、有明确 reason 的
        "pod_issues": sorted(
            pod_issues.values(),
            key=lambda r: (bool(r.get("reasons")), r.get("restarts") or 0),
            reverse=True,
        )[:20],
        "event_count": len(pod_events),
    }
