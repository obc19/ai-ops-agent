"""Jaeger 适配器：只读 trace，定位耗时异常的服务。默认关闭（ENABLE_TRACE_AGENT）。"""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, TraceEvent
from app.utils.logging import get_logger

logger = get_logger(__name__)


class JaegerCollector(BaseCollector):
    name = "jaeger"
    source = SourceType.TRACE

    @property
    def base_url(self) -> str:
        return (self.sources.jaeger_url if self.sources else "") or self.settings.jaeger_url

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def enabled(self) -> bool:
        return self.settings.enable_trace_agent

    async def query(self, window) -> list[BaseEvent]:
        if not self.enabled:
            return []
        if self.use_mock:
            return self._mock(window)
        return await self._real(window)

    def _mock(self, window) -> list[TraceEvent]:
        return [
            TraceEvent(
                cluster=world.cluster,
                namespace=world.namespace,
                target=row["service"],
                service=row["service"],
                operation=row["operation"],
                trace_id=row["trace_id"],
                duration_ms=row["duration_ms"],
                span_count=row["span_count"],
                error=row["error"],
            )
            for row in world.traces(window)
        ]

    async def _real(self, window) -> list[TraceEvent]:
        start_us = int(window.start.timestamp() * 1_000_000)
        end_us = int(window.end.timestamp() * 1_000_000)
        events: list[TraceEvent] = []
        async with self._client(self.base_url) as client:
            services = (await client.get("/api/services")).json().get("data") or []
            for service in services[:20]:
                resp = await client.get(
                    "/api/traces",
                    params={"service": service, "start": start_us, "end": end_us, "limit": 20},
                )
                if resp.status_code != 200:
                    continue
                for trace in resp.json().get("data") or []:
                    spans = trace.get("spans") or []
                    durations = [s.get("duration", 0) for s in spans]
                    if not durations:
                        continue
                    errors = any(
                        tag.get("key") == "error" and tag.get("value") is True
                        for span in spans
                        for tag in span.get("tags", [])
                    )
                    events.append(
                        TraceEvent(
                            cluster=self.cluster_name,
                            namespace="default",
                            target=service,
                            service=service,
                            operation=spans[0].get("operationName", ""),
                            trace_id=trace.get("traceID", ""),
                            duration_ms=round(max(durations) / 1000, 2),
                            span_count=len(spans),
                            error=errors,
                        )
                    )
        logger.info("Jaeger 返回 %d 条链路", len(events))
        return events

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover
        async with self._client(self.base_url) as client:
            resp = await client.get("/api/services")
            return DataSourceHealth(
                name=self.name,
                configured=True,
                reachable=resp.status_code == 200,
                detail=f"HTTP {resp.status_code}",
            )

    # ---------- 拓扑：从 span 父子关系抽调用边 ----------
    async def fetch_edges(self, window, force: bool = False) -> list[tuple[str, str, int, int]]:
        """抽取服务间调用边，返回 ``[(caller, callee, calls, error_calls)]``。

        依据是 Jaeger 的 span 结构：每个 span 有 ``processID``（指向
        ``trace.processes[processID].serviceName``），并有 ``references``
        指向父 span 的 ``spanID``。若父子分属不同服务，就是一条跨服务调用边。

        同一个服务内部的 span 父子关系不算依赖，必须剔除。

        ``force=True`` 时忽略 ``ENABLE_TRACE_AGENT``：拓扑推导与「链路 Agent
        是否参与诊断」是两件事，只要 Jaeger 可达就有价值。
        """
        if not (self.enabled or force):
            return []
        if self.use_mock:
            return world.dependency_edges()

        start_us = int(window.start.timestamp() * 1_000_000)
        end_us = int(window.end.timestamp() * 1_000_000)
        counts: dict[tuple[str, str], list[int]] = {}

        async with self._client(self.base_url) as client:
            services = (await client.get("/api/services")).json().get("data") or []
            for service in services[:30]:
                resp = await client.get(
                    "/api/traces",
                    params={"service": service, "start": start_us, "end": end_us, "limit": 20},
                )
                if resp.status_code != 200:
                    continue
                for trace in resp.json().get("data") or []:
                    spans = trace.get("spans") or []
                    processes = trace.get("processes") or {}
                    # spanID → 所属服务
                    owner: dict[str, str] = {}
                    for span in spans:
                        proc = processes.get(span.get("processID", "")) or {}
                        svc = proc.get("serviceName") or ""
                        if svc:
                            owner[span.get("spanID", "")] = svc
                    for span in spans:
                        callee = owner.get(span.get("spanID", ""))
                        if not callee:
                            continue
                        errored = any(
                            tag.get("key") == "error" and tag.get("value") is True
                            for tag in span.get("tags", [])
                        )
                        for ref in span.get("references") or []:
                            if ref.get("refType") not in (None, "CHILD_OF"):
                                continue
                            caller = owner.get(ref.get("spanID", ""))
                            if not caller or caller == callee:
                                continue
                            slot = counts.setdefault((caller, callee), [0, 0])
                            slot[0] += 1
                            if errored:
                                slot[1] += 1

        edges = [(caller, callee, v[0], v[1]) for (caller, callee), v in counts.items()]
        logger.info("Jaeger 抽取到 %d 条服务调用边", len(edges))
        return edges
