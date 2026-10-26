"""Kubernetes 适配器：只读集群事件（Pod 重启、调度失败等）。默认关闭。"""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, K8sEvent
from app.utils.logging import get_logger

logger = get_logger(__name__)


class K8sCollector(BaseCollector):
    name = "kubernetes"
    source = SourceType.K8S

    @property
    def base_url(self) -> str:
        return (
            (self.sources.kubernetes_api_url if self.sources else "")
            or self.settings.kubernetes_api_url
        )

    @property
    def token(self) -> str:
        return (
            (self.sources.kubernetes_token if self.sources else "")
            or self.settings.kubernetes_token
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def enabled(self) -> bool:
        return self.settings.enable_k8s_adapter

    async def query(self, window) -> list[BaseEvent]:
        if not self.enabled:
            return []
        if self.use_mock:
            return self._mock(window)
        return await self._real(window)

    def _mock(self, window) -> list[K8sEvent]:
        return [
            K8sEvent(
                cluster=world.cluster,
                namespace=world.namespace,
                target=row["target"],
                reason=row["reason"],
                kind=row["kind"],
                message=row["message"],
                count=row["count"],
            )
            for row in world.k8s_events(window)
        ]

    async def _real(self, window) -> list[K8sEvent]:
        headers = {"Authorization": f"Bearer {self.token}"}
        events: list[K8sEvent] = []
        async with self._client(self.base_url) as client:
            resp = await client.get(
                "/api/v1/events", params={"fieldSelector": "type!=Normal"}, headers=headers
            )
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                meta = item.get("metadata", {})
                events.append(
                    K8sEvent(
                        cluster=self.cluster_name,
                        namespace=meta.get("namespace", "default"),
                        target=(item.get("involvedObject") or {}).get("name", "unknown"),
                        reason=item.get("reason", ""),
                        kind=(item.get("involvedObject") or {}).get("kind", ""),
                        message=item.get("message", "")[:1000],
                        count=int(item.get("count", 1) or 1),
                    )
                )
        logger.info("Kubernetes 返回 %d 条事件", len(events))
        return events

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self._client(self.base_url) as client:
            resp = await client.get("/version", headers=headers)
            return DataSourceHealth(
                name=self.name,
                configured=True,
                reachable=resp.status_code == 200,
                detail=f"HTTP {resp.status_code}",
            )
