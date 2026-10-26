"""采集适配器基类：只读、无状态、未配置自动跳过。"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from app.config.settings import Settings, get_settings
from app.models.enums import SourceType
from app.models.events import BaseEvent, DataSourceHealth
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)


class BaseCollector(ABC):
    """新增数据源只需继承本类并实现 query / configured。"""

    name: str = "base"
    source: SourceType = SourceType.METRIC

    def __init__(
        self, settings: Settings | None = None, cluster=None
    ) -> None:
        self.settings = settings or get_settings()
        if cluster is None:
            from app.config.clusters import default_cluster

            cluster = default_cluster(self.settings)
        self.cluster = cluster

    # ---------- 集群上下文 ----------
    @property
    def cluster_name(self) -> str:
        return getattr(self.cluster, "name", "default")

    @property
    def namespace(self) -> str:
        return getattr(self.cluster, "namespace", "default")

    @property
    def sources(self):
        return getattr(self.cluster, "sources", None)

    @property
    def label(self) -> str:
        """多集群下用于区分同名适配器。"""
        return f"{self.name}@{self.cluster_name}"

    # ---------- 子类实现 ----------
    @property
    @abstractmethod
    def configured(self) -> bool:
        """是否配置了真实数据源地址。"""

    @abstractmethod
    async def query(self, window) -> list[BaseEvent]:
        """按时间窗口只读拉取，返回标准化事件。"""

    @property
    def use_mock(self) -> bool:
        return self.settings.mock_mode or not self.configured

    # ---------- 通用能力 ----------
    def _client(self, base_url: str, verify: bool | str | None = None) -> httpx.AsyncClient:
        """trust_env=False：容器内忽略 HTTP(S)_PROXY，避免集群内访问被代理劫持。"""
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=6.0,
            follow_redirects=True,
            trust_env=False,
            verify=self.settings.tls_verify if verify is None else verify,
        )

    async def health_check(self) -> DataSourceHealth:
        if not self.configured and not self.settings.mock_mode:
            return DataSourceHealth(
                name=self.label, configured=False, reachable=False, detail="未配置，已跳过"
            )
        if self.use_mock:
            return DataSourceHealth(
                name=self.label, configured=self.configured, reachable=True, detail="mock 数据源"
            )
        try:
            return await self._probe()
        except Exception as exc:  # noqa: BLE001 - 探活失败不应中断主流程
            logger.warning("%s 探活失败：%s", self.name, exc)
            return DataSourceHealth(
                name=self.label,
                configured=True,
                reachable=False,
                detail=str(exc)[:200],
                checked_at=utc_now(),
            )

    async def _probe(self) -> DataSourceHealth:  # pragma: no cover - 真实环境才用
        raise NotImplementedError

    async def safe_query(self, window) -> list[BaseEvent]:
        """带重试与降级的查询：失败三次后返回空列表，不阻断整条链路。"""
        import asyncio

        for attempt in range(3):
            try:
                return await self.query(window)
            except Exception as exc:  # noqa: BLE001
                wait = 0.2 * (2**attempt)
                logger.warning(
                    "%s 第 %d 次查询失败（%s），%.1fs 后重试", self.name, attempt + 1, exc, wait
                )
                if attempt == 2:
                    break
                await asyncio.sleep(wait)
        return []
