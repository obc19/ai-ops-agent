"""Prometheus 适配器：只读 query_range，输出 MetricEvent。"""

from __future__ import annotations

from datetime import datetime

from app.collectors.base import BaseCollector
from app.collectors.queries import METRIC_QUERIES
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, MetricEvent, MetricSample
from app.utils.logging import get_logger

logger = get_logger(__name__)


class PrometheusCollector(BaseCollector):
    name = "prometheus"
    source = SourceType.METRIC

    @property
    def base_url(self) -> str:
        return (self.sources.prometheus_url if self.sources else "") or self.settings.prometheus_url

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def query(self, window) -> list[BaseEvent]:
        if self.use_mock:
            return self._mock(window)
        return await self._real(window)

    # ---------- mock ----------
    def _mock(self, window) -> list[MetricEvent]:
        events: list[MetricEvent] = []
        for target in world.targets:
            for metric, spec in METRIC_QUERIES.items():
                series = world.metric_series(target, metric, window)
                events.append(
                    MetricEvent(
                        cluster=world.cluster,
                        namespace=world.namespace,
                        target=target,
                        metric=metric,
                        unit=spec["unit"],
                        value=series[-1][1] if series else None,
                        samples=[MetricSample(timestamp=ts, value=v) for ts, v in series],
                    )
                )
        return events

    # ---------- real ----------
    async def _real(self, window) -> list[MetricEvent]:
        events: list[MetricEvent] = []
        start = window.start.timestamp()
        end = window.end.timestamp()
        step = max(int((end - start) / 24), 15)

        async with self._client(self.base_url) as client:
            for metric, spec in METRIC_QUERIES.items():
                resp = await client.get(
                    "/api/v1/query_range",
                    params={"query": spec["query"], "start": start, "end": end, "step": step},
                )
                resp.raise_for_status()
                payload = resp.json()
                for result in payload.get("data", {}).get("result", []):
                    labels = result.get("metric", {})
                    target = labels.get("pod") or labels.get("instance") or "unknown"
                    samples = [
                        MetricSample(
                            timestamp=datetime.fromtimestamp(float(ts)),
                            value=float(val),
                        )
                        for ts, val in result.get("values", [])
                        if val not in ("NaN", "+Inf", "-Inf")
                    ]
                    if not samples:
                        continue
                    events.append(
                        MetricEvent(
                            cluster=self.cluster_name,
                            namespace=labels.get("namespace") or self.namespace,
                            target=target,
                            metric=metric,
                            unit=spec["unit"],
                            labels=labels,
                            value=samples[-1].value,
                            samples=samples,
                        )
                    )
        logger.info("Prometheus 返回 %d 条指标序列", len(events))
        return events

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover
        async with self._client(self.base_url) as client:
            resp = await client.get("/-/healthy")
            return DataSourceHealth(
                name=self.name,
                configured=True,
                reachable=resp.status_code == 200,
                detail=f"HTTP {resp.status_code}",
            )
