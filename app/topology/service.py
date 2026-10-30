"""拓扑服务：缓存与刷新服务依赖图。

数据来源有两路，按优先级合并而非互斥：

1. **链路推导**（首选）：Jaeger span 的父子关系，自动、无需人工维护。
2. **配置声明**（兜底）：`topology.dependencies`，用于 trace 采样率低、
   或是异步调用（MQ、定时任务）这类在 trace 里看不到的依赖。

两者都没有时返回空图 —— 关联收敛会自动退化为「不收敛」，绝不瞎猜。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.config.settings import Settings, get_settings
from app.models.events import TimeWindow
from app.models.topology import ServiceGraph, TopologyEdge
from app.topology.builder import build_graph, parse_manual
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 拓扑回看窗口要比检测窗口长得多：低频调用（如每小时一次的批处理）
# 在 5 分钟窗口里根本看不到，拓扑会漏边。
TOPOLOGY_LOOKBACK = timedelta(hours=2)


class TopologyService:
    """进程内单例，与调度器同生命周期。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._graph: ServiceGraph | None = None
        self._expires_at = None
        self._lock = asyncio.Lock()

    # ---------- 对外 ----------
    async def current(self, force: bool = False) -> ServiceGraph:
        """取当前图；缓存过期则自动刷新。"""
        if not self.settings.topology_enabled:
            return ServiceGraph(source="empty", note="拓扑已关闭（topology.enabled=false）")
        if not force and self._cached_valid():
            return self._graph  # type: ignore[return-value]
        async with self._lock:
            if not force and self._cached_valid():
                return self._graph  # type: ignore[return-value]
            return await self._refresh_locked()

    async def refresh(self) -> ServiceGraph:
        async with self._lock:
            return await self._refresh_locked()

    def cached(self) -> ServiceGraph | None:
        return self._graph

    # ---------- 内部 ----------
    def _cached_valid(self) -> bool:
        if self._graph is None:
            return False
        ttl = self.settings.topology_refresh_seconds
        if ttl <= 0:
            return False  # 0 = 不缓存，每次都刷新
        return self._expires_at is not None and utc_now() < self._expires_at

    async def _refresh_locked(self) -> ServiceGraph:
        sources: list[str] = []
        notes: list[str] = []
        edges: list[TopologyEdge] = []

        # 1) 链路推导 / mock 世界
        trace_edges = await self._trace_edges(notes)
        if trace_edges:
            sources.append("trace")
            edges.extend(
                TopologyEdge(caller=c, callee=e, calls=max(calls, 1), error_calls=err)
                for c, e, calls, err in trace_edges
            )

        # 2) 配置声明（只补 trace 里没有的边）
        manual = parse_manual(self.settings.topology_dependencies)
        if manual:
            sources.append("config")
            seen = {(e.caller, e.callee) for e in edges}
            for caller, callee in manual:
                if (caller, callee) not in seen:
                    edges.append(TopologyEdge(caller=caller, callee=callee, calls=0))
                    seen.add((caller, callee))

        graph = build_graph(
            edges,
            source="+".join(sources) or "empty",
            note="；".join(notes),
        )
        self._graph = graph
        ttl = self.settings.topology_refresh_seconds
        self._expires_at = utc_now() + timedelta(seconds=ttl) if ttl > 0 else None

        if graph.is_empty():
            logger.info("服务拓扑为空：未开启链路推导，也未配置 topology.dependencies")
        else:
            logger.info(
                "服务拓扑已刷新：%d 节点 / %d 边（来源 %s）",
                len(graph.nodes),
                len(graph.edges),
                graph.source,
            )
        return graph

    async def _trace_edges(self, notes: list[str]) -> list[tuple[str, str, int, int]]:
        # mock 模式直接给一份确定性的图，否则演示时永远收敛不了
        if self.settings.mock_mode:
            from app.mock import world

            return world.dependency_edges()

        try:
            from app.collectors.jaeger_adapter import JaegerCollector

            collector = JaegerCollector(self.settings)
            if not collector.configured:
                notes.append("未配置 JAEGER_URL，跳过链路推导")
                return []
            window = TimeWindow(
                start=utc_now() - TOPOLOGY_LOOKBACK,
                end=utc_now(),
            )
            return await collector.fetch_edges(window, force=True)
        except Exception as exc:  # noqa: BLE001 - 拓扑失败不得影响主链路
            logger.warning("从链路推导拓扑失败：%s", exc)
            notes.append(f"链路推导失败：{type(exc).__name__}")
            return []


_topology: TopologyService | None = None


def get_topology() -> TopologyService:
    global _topology
    if _topology is None:
        _topology = TopologyService()
    return _topology


def reset_topology() -> None:
    """测试用：清掉单例，避免缓存串场。"""
    global _topology
    _topology = None
