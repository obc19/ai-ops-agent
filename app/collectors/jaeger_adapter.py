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
