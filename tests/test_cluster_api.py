"""集群状态接口：没装 Prometheus / ELK 时，K8s 原生信号也要能变成可读的结论。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.collectors import build_collectors
from app.config.settings import get_settings
from app.main import app
from app.mock import world

client = TestClient(app)


def test_cluster_reports_disabled_by_default(monkeypatch):
    """默认既不集群内、也没开开关 —— 要明确说「没启用」，不能装作没数据。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_k8s_adapter", False)
    body = client.get("/api/v1/cluster").json()
    assert body["available"] is False
    assert "未启用" in body["reason"]


def test_cluster_returns_mock_pod_issues(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_k8s_adapter", True)
    world.inject_fault("order-service", metric="cpu_usage_percent", severity="P0")

    body = client.get("/api/v1/cluster").json()
    assert body["available"] is True
    assert body["in_cluster"] is False
    # mock 世界没有节点/工作负载，但 Pod 级事件必须有
    assert body["pod_issues"], body
    pod = body["pod_issues"][0]
    assert pod["name"] == "order-service"
    assert pod["reasons"]


def test_k8s_adapter_joins_registry_when_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_k8s_adapter", True)
    names = [c.name for c in build_collectors(settings)]
    assert "kubernetes" in names

    monkeypatch.setattr(settings, "enable_k8s_adapter", False)
    names = [c.name for c in build_collectors(settings)]
    assert "kubernetes" not in names


@pytest.mark.parametrize(
    ("reason", "expected_rule"),
    [
        ("CrashLoopBackOff", "k8s-crashloop"),
        ("OOMKilled", "k8s-oomkilled"),
        ("FailedScheduling", "k8s-unschedulable"),
        ("NodeNotReady", "k8s-node-not-ready"),
    ],
)
def test_real_rules_cover_common_failures(reason, expected_rule):
    """规则文件里的 reason 白名单要真的能命中，别写完规则忘了对名字。"""
    from datetime import timedelta

    from app.detection.engine import get_engine
    from app.models.events import K8sEvent, TimeWindow
    from app.utils.clock import utc_now

    now = utc_now()
    window = TimeWindow(start=now - timedelta(minutes=5), end=now)
    anomalies = get_engine().detect(
        [
            K8sEvent(
                namespace="prod", target="api-1", reason=reason, kind="Pod", message="x", count=2
            )
        ],
        window,
    )
    assert [a.metric for a in anomalies] == [expected_rule]
