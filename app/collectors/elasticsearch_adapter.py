"""Elasticsearch 适配器：只读检索异常时段日志（采集端由运维已有 ELK 提供）。"""

from __future__ import annotations

import base64

from app.collectors.base import BaseCollector
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, LogEvent
from app.utils.clock import iso_utc
from app.utils.logging import get_logger

logger = get_logger(__name__)

ERROR_LEVELS = ["ERROR", "WARN", "FATAL", "CRITICAL"]


class ElasticsearchCollector(BaseCollector):
    name = "elasticsearch"
    source = SourceType.LOG

    @property
    def configured(self) -> bool:
        return bool(self.settings.elasticsearch_url)

    async def query(self, window) -> list[BaseEvent]:
        if self.use_mock:
            return self._mock(window)
        return await self._real(window)

    # ---------- mock ----------
    def _mock(self, window) -> list[LogEvent]:
        events = []
        for row in world.logs(window):
            events.append(
                LogEvent(
                    cluster=world.cluster,
                    namespace=world.namespace,
                    target=row["service"],
                    index=self.settings.elasticsearch_index,
                    level=row["level"],
                    message=row["message"],
                    count=row["count"],
                )
            )
        return events

    # ---------- real ----------
    def _auth_headers(self) -> dict[str, str]:
        if not self.settings.elasticsearch_username:
            return {}
        raw = f"{self.settings.elasticsearch_username}:{self.settings.elasticsearch_password}"
        token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    async def _real(self, window) -> list[LogEvent]:
        body = {
            "size": 100,
            "sort": [{"@timestamp": "desc"}],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": iso_utc(window.start), "lte": iso_utc(window.end)}}}
                    ],
                    "must": [{"terms": {"level": ERROR_LEVELS}}],
                }
            },
        }
        url = f"/{self.settings.elasticsearch_index}/_search"
        async with self._client(self.settings.elasticsearch_url) as client:
            resp = await client.post(url, json=body, headers=self._auth_headers())
            resp.raise_for_status()
            payload = resp.json()

        events: list[LogEvent] = []
        for hit in payload.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            events.append(
                LogEvent(
                    cluster=src.get("cluster", self.settings.app_name),
                    namespace=src.get("namespace", "default"),
                    target=src.get("service") or src.get("pod") or "unknown",
                    index=hit.get("_index", ""),
                    level=str(src.get("level", "ERROR")),
                    message=str(src.get("message", ""))[:2000],
                    count=int(src.get("count", 1) or 1),
                )
            )
        logger.info("Elasticsearch 返回 %d 条日志", len(events))
        return events

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover
        async with self._client(self.settings.elasticsearch_url) as client:
            resp = await client.get("/_cluster/health", headers=self._auth_headers())
            data = resp.json() if resp.status_code == 200 else {}
            return DataSourceHealth(
                name=self.name,
                configured=True,
                reachable=resp.status_code == 200,
                detail=f"status={data.get('status', 'unknown')}",
            )
