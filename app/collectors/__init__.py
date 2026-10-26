"""采集器注册表：按配置组装可用适配器，未配置/未启用的自动跳过。"""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.collectors.elasticsearch_adapter import ElasticsearchCollector
from app.collectors.jaeger_adapter import JaegerCollector
from app.collectors.k8s_adapter import K8sCollector
from app.collectors.prometheus_adapter import PrometheusCollector
from app.config.settings import Settings, get_settings
from app.models.events import BaseEvent, DataSourceHealth
from app.utils.logging import get_logger

logger = get_logger(__name__)

_REGISTRY: dict[str, type[BaseCollector]] = {
    "prometheus": PrometheusCollector,
    "elasticsearch": ElasticsearchCollector,
    "jaeger": JaegerCollector,
    "kubernetes": K8sCollector,
}


def build_collectors(settings: Settings | None = None) -> list[BaseCollector]:
    """按集群实例化适配器：多集群配置下每个集群各一组。"""
    from app.config.clusters import load_clusters

    settings = settings or get_settings()
    clusters = load_clusters(settings)
    if settings.mock_mode:
        # mock 世界只模拟一个集群，多集群留给真实环境验证
        clusters = clusters[:1]

    collectors: list[BaseCollector] = []
    for cluster in clusters:
        if not cluster.enabled:
            logger.info("集群 %s 已禁用，跳过", cluster.name)
            continue
        for name, cls in _REGISTRY.items():
            collector = cls(settings, cluster)
            if not getattr(collector, "enabled", True):
                logger.debug("适配器 %s@%s 未启用，跳过", name, cluster.name)
                continue
            if not collector.configured and not settings.mock_mode:
                logger.info("适配器 %s@%s 未配置地址，跳过", name, cluster.name)
                continue
            collectors.append(collector)
    logger.info("启用采集适配器：%s", [c.label for c in collectors])
    return collectors


async def collect_all(window, settings: Settings | None = None) -> list[BaseEvent]:
    """并发拉取全部数据源，单源失败不影响其他源。"""
    import asyncio

    collectors = build_collectors(settings)
    results = await asyncio.gather(*(c.safe_query(window) for c in collectors))
    events: list[BaseEvent] = []
    for batch in results:
        events.extend(batch)
    return events


async def collectors_health(
    settings: Settings | None = None, cluster: str | None = None
) -> list[DataSourceHealth]:
    import asyncio

    collectors = build_collectors(settings)
    if cluster:
        collectors = [c for c in collectors if c.cluster_name == cluster]
    return list(await asyncio.gather(*(c.health_check() for c in collectors)))
