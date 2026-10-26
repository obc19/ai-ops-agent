"""Kubernetes 适配器：只读集群事件（Pod 重启、调度失败等）。默认关闭。

容器跑在集群内（Deployment）时无需任何配置：自动读取 ServiceAccount 挂载的
``/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}``，直连
``https://kubernetes.default.svc``。集群外运行时仍用环境变量显式配置。
"""

from __future__ import annotations

from pathlib import Path

from app.collectors.base import BaseCollector
from app.mock import world
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth, K8sEvent
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 标准挂载路径，由 kubelet 注入
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IN_CLUSTER_API = "https://kubernetes.default.svc"


def _read_sa_file(name: str) -> str:
    """读取 SA 挂载文件；不存在或不可读时返回空串。"""
    candidate = SA_DIR / name
    try:
        return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class K8sCollector(BaseCollector):
    name = "kubernetes"
    source = SourceType.K8S

    @property
    def in_cluster(self) -> bool:
        return SA_DIR.joinpath("token").exists()

    @property
    def base_url(self) -> str:
        explicit = (
            (self.sources.kubernetes_api_url if self.sources else "")
            or self.settings.kubernetes_api_url
        )
        if explicit:
            return explicit
        # 集群内未配地址时，直连内置的服务入口
        return IN_CLUSTER_API if self.in_cluster else ""

    @property
    def token(self) -> str:
        explicit = (
            (self.sources.kubernetes_token if self.sources else "")
            or self.settings.kubernetes_token
        )
        if explicit:
            return explicit
        if self.settings.kubernetes_token_file:
            try:
                return Path(self.settings.kubernetes_token_file).read_text(encoding="utf-8").strip()
            except OSError:
                logger.warning("KUBERNETES_TOKEN_FILE 不可读：%s", self.settings.kubernetes_token_file)
                return ""
        return _read_sa_file("token")

    @property
    def ca_file(self) -> str:
        """私有 CA 路径：显式配置优先，否则用 SA 挂载的集群 CA。"""
        explicit = self.settings.kubernetes_ca_file
        if explicit:
            return explicit
        candidate = SA_DIR / "ca.crt"
        return str(candidate) if candidate.exists() else ""

    @property
    def verify(self) -> bool | str:
        if not self.settings.tls_verify:
            return False
        return self.ca_file or True

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
        async with self._client(self.base_url, verify=self.verify) as client:
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
        async with self._client(self.base_url, verify=self.verify) as client:
            resp = await client.get("/version", headers=headers)
            return DataSourceHealth(
                name=self.name,
                configured=True,
                reachable=resp.status_code == 200,
                detail=f"HTTP {resp.status_code}",
            )
