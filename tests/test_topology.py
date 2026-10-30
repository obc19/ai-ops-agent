"""服务拓扑与根因收敛。

重点验证两件事：
1. 图本身的正确性（拓扑序、上下游、环）；
2. 收敛**不能误判**——无关故障必须保持独立，无拓扑时必须退化。
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, TimeWindow
from app.models.topology import TopologyEdge
from app.topology import TopologyService, build_graph, correlate
from app.topology.builder import parse_manual
from app.utils.clock import utc_now

CHAIN = [
    ("api-gateway", "order-service"),
    ("order-service", "payment-service"),
    ("order-service", "user-service"),
]


def _window() -> TimeWindow:
    now = utc_now()
    return TimeWindow(start=now, end=now)


def _anomaly(target: str, metric: str = "http_latency_p99_ms", severity=Severity.P1) -> AnomalyEvent:
    return AnomalyEvent(
        source=SourceType.METRIC,
        target=target,
        metric=metric,
        severity=severity,
        window=_window(),
        summary=f"{target} {metric} 异常",
    )


def _settings(tmp_path, **overrides) -> Settings:
    values = {"mock_mode": False, "data_dir": tmp_path, "topology_dependencies": {}}
    values.update(overrides)
    return Settings(**values)


# ---------------- 图构建 ----------------
def test_build_graph_merges_duplicates_and_drops_self_loop():
    graph = build_graph(
        [
            ("a", "b"),
            ("a", "b"),
            ("b", "b"),  # 自环不是依赖
            ("b", "c"),
        ]
    )
    assert len(graph.edges) == 2
    assert "b" not in graph.downstream_of("b")
    ab = next(e for e in graph.edges if e.callee == "b")
    assert ab.calls >= 1


def test_build_graph_accepts_topology_edge_objects():
    graph = build_graph(
        [
            TopologyEdge(caller="x", callee="y", calls=10, error_calls=3),
            TopologyEdge(caller="x", callee="y", calls=5, error_calls=1),
        ]
    )
    edge = graph.edges[0]
    assert edge.calls == 15
    assert edge.error_calls == 4


def test_graph_queries():
    graph = build_graph(CHAIN)
    assert graph.roots() == ["api-gateway"]
    assert graph.leaves() == ["payment-service", "user-service"]
    assert graph.upstream_of("order-service") == ["api-gateway"]
    assert graph.downstream_of("order-service") == ["payment-service", "user-service"]
    assert graph.ancestors("payment-service") == ["api-gateway", "order-service"]
    assert graph.descendants("api-gateway") == [
        "order-service",
        "payment-service",
        "user-service",
    ]
    assert graph.topological_order().index("api-gateway") < graph.topological_order().index(
        "payment-service"
    )


def test_cycle_detected_and_does_not_hang():
    graph = build_graph([("a", "b"), ("b", "c"), ("c", "a")])
    assert graph.has_cycle()
    assert set(graph.find_cycle()[:3]) == {"a", "b", "c"}
    # 有环时也要能给出顺序，不能抛异常
    assert len(graph.topological_order()) == 3


def test_render_ascii_marks_cycle():
    graph = build_graph([("a", "b"), ("b", "a")])
    text = graph.render_ascii()
    assert "a" in text and "b" in text


def test_parse_manual_supports_both_shapes():
    mapping_shape = parse_manual({"order": ["pay", "user"]})
    list_shape = parse_manual([{"caller": "order", "callee": "pay"}])
    assert ("order", "pay") in mapping_shape and ("order", "user") in mapping_shape
    assert list_shape == [("order", "pay")]
    assert parse_manual(None) == []


# ---------------- 收敛 ----------------
def test_full_chain_collapses_to_single_root():
    graph = build_graph(CHAIN)
    clusters = correlate([_anomaly(t) for t in ["api-gateway", "order-service", "payment-service", "user-service"]], graph)
    assert len(clusters) == 1
    assert clusters[0].root.target == "api-gateway"
    assert set(clusters[0].impact) == {"order-service", "payment-service", "user-service"}
    assert clusters[0].suppressed == 3


def test_partial_chain_root_is_highest_failing_node():
    graph = build_graph(CHAIN)
    clusters = correlate([_anomaly(t) for t in ["order-service", "payment-service"]], graph)
    assert len(clusters) == 1
    assert clusters[0].root.target == "order-service"


def test_unrelated_failures_stay_separate():
    """两个没有依赖关系的服务同时异常，绝不能被硬并成一个。"""
    graph = build_graph(CHAIN)
    clusters = correlate([_anomaly("payment-service"), _anomaly("user-service")], graph)
    assert len(clusters) == 2


def test_empty_graph_never_guesses():
    graph = build_graph([])
    clusters = correlate([_anomaly("order-service"), _anomaly("payment-service")], graph)
    assert len(clusters) == 2
    assert all(c.topology_used is False for c in clusters)
    assert all("无拓扑" in c.reason for c in clusters)


def test_same_target_metrics_merge_into_one():
    graph = build_graph(CHAIN)
    clusters = correlate(
        [
            _anomaly("payment-service", "cpu_usage_percent", Severity.P2),
            _anomaly("payment-service", "http_latency_p99_ms", Severity.P1),
            _anomaly("payment-service", "http_error_rate_percent", Severity.P0),
        ],
        graph,
    )
    assert len(clusters) == 1
    assert clusters[0].root.severity is Severity.P0
    assert clusters[0].suppressed == 2


def test_root_severity_escalates_by_impact():
    graph = build_graph(CHAIN)
    clusters = correlate(
        [_anomaly("order-service", severity=Severity.P2), _anomaly("payment-service", severity=Severity.P0)],
        graph,
    )
    assert len(clusters) == 1
    assert clusters[0].root.target == "order-service"
    assert clusters[0].root.severity is Severity.P0


def test_escalation_can_be_disabled():
    graph = build_graph(CHAIN)
    clusters = correlate(
        [_anomaly("order-service", severity=Severity.P2), _anomaly("payment-service", severity=Severity.P0)],
        graph,
        escalate_by_impact=False,
    )
    assert clusters[0].root.severity is Severity.P2


def test_target_not_in_graph_stands_alone():
    """主机类对象不在服务图里，不能被硬塞进任何调用链。"""
    graph = build_graph(CHAIN)
    clusters = correlate([_anomaly("order-service"), _anomaly("node-1")], graph)
    assert len(clusters) == 2
    assert {c.root.target for c in clusters} == {"order-service", "node-1"}


def test_empty_input_returns_empty():
    assert correlate([], build_graph(CHAIN)) == []


def test_cluster_to_public_is_json_safe():
    graph = build_graph(CHAIN)
    cluster = correlate([_anomaly("order-service"), _anomaly("payment-service")], graph)[0]
    payload = cluster.to_public()
    assert payload["root"]["target"] == "order-service"
    assert payload["suppressed"] == 1
    assert isinstance(payload["members"], list)


# ---------------- 拓扑服务 ----------------
@pytest.mark.asyncio
async def test_service_uses_configured_dependencies(tmp_path):
    settings = _settings(tmp_path, topology_dependencies={"a": ["b"], "b": ["c"]})
    service = TopologyService(settings)
    graph = await service.current()
    assert graph.source == "config"
    assert graph.downstream_of("a") == ["b"]


@pytest.mark.asyncio
async def test_service_returns_empty_without_any_source(tmp_path):
    settings = _settings(tmp_path)
    service = TopologyService(settings)
    graph = await service.current()
    assert graph.is_empty()
    assert graph.source == "empty"


@pytest.mark.asyncio
async def test_service_disabled_skips_everything(tmp_path):
    settings = _settings(tmp_path, topology_enabled=False, topology_dependencies={"a": ["b"]})
    service = TopologyService(settings)
    graph = await service.current()
    assert graph.is_empty()
    assert "关闭" in graph.note


@pytest.mark.asyncio
async def test_service_mock_mode_provides_demo_graph(tmp_path):
    settings = Settings(mock_mode=True, data_dir=tmp_path)
    service = TopologyService(settings)
    graph = await service.current()
    assert not graph.is_empty()
    assert "order-service" in graph.nodes


# ---------------- 接口 ----------------
def test_topology_endpoint_is_registered_and_reachable():
    """回归：router 内不要再写 /api/v1 前缀（main 已统一加），否则会变成 /api/v1/api/v1/..."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    # 先确认没有被重复前缀污染
    openapi = client.get("/openapi.json").json()["paths"]
    assert not [p for p in openapi if "/api/v1/api/v1" in p]

    resp = client.get("/api/v1/topology")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data and "edges" in data
    assert "enabled" in data
