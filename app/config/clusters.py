"""多集群配置：一份 YAML 描述多个集群的数据源，适配器按集群分别实例化。

未配置 ``CLUSTERS_CONFIG`` 时退化为单集群，使用环境变量里的单一数据源地址，
与 v0.1 行为完全一致。

配置示例（可放文件，也可直接写进环境变量）::

    clusters:
      - name: prod-cluster
        namespace: prod
        prometheus_url: http://prometheus.prod:9090
        elasticsearch_url: http://es.prod:9200
        jaeger_url: http://jaeger.prod:16686
        kubernetes_api_url: https://k8s.prod:6443
      - name: staging
        namespace: staging
        prometheus_url: http://prometheus.staging:9090
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config.settings import Settings, get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ClusterSources:
    """单集群的只读数据源地址。"""

    prometheus_url: str = ""
    elasticsearch_url: str = ""
    elasticsearch_index: str = ""
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    jaeger_url: str = ""
    kubernetes_api_url: str = ""
    kubernetes_token: str = ""

    @property
    def configured_sources(self) -> list[str]:
        pairs = (
            ("prometheus", self.prometheus_url),
            ("elasticsearch", self.elasticsearch_url),
            ("jaeger", self.jaeger_url),
            ("kubernetes", self.kubernetes_api_url),
        )
        return [name for name, url in pairs if url]


@dataclass
class ClusterConfig:
    name: str = "default"
    namespace: str = "default"
    enabled: bool = True
    sources: ClusterSources = field(default_factory=ClusterSources)


def default_cluster(settings: Settings | None = None) -> ClusterConfig:
    cfg = settings or get_settings()
    return ClusterConfig(
        name=cfg.default_cluster,
        namespace=cfg.default_namespace,
        sources=ClusterSources(
            prometheus_url=cfg.prometheus_url,
            elasticsearch_url=cfg.elasticsearch_url,
            elasticsearch_index=cfg.elasticsearch_index,
            elasticsearch_username=cfg.elasticsearch_username,
            elasticsearch_password=cfg.elasticsearch_password,
            jaeger_url=cfg.jaeger_url,
            kubernetes_api_url=cfg.kubernetes_api_url,
            kubernetes_token=cfg.kubernetes_token,
        ),
    )


def _read_yaml(raw: str) -> dict:
    """CLUSTERS_CONFIG 既支持文件路径，也支持直接写 YAML 文本。"""
    path = Path(raw)
    if "\n" not in raw and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return yaml.safe_load(raw) or {}


def load_clusters(settings: Settings | None = None) -> list[ClusterConfig]:
    cfg = settings or get_settings()
    raw = cfg.clusters_config.strip()
    if not raw:
        # 未显式配置时，配置目录下的 clusters.yaml 存在即生效（挂载即可用）
        auto = cfg.clusters_file
        if auto is None:
            return [default_cluster(cfg)]
        raw = str(auto)

    try:
        payload = _read_yaml(raw)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("多集群配置解析失败，回退单集群：%s", exc)
        return [default_cluster(cfg)]

    items = payload.get("clusters") or []
    if not items:
        logger.warning("多集群配置为空，回退单集群")
        return [default_cluster(cfg)]

    base = default_cluster(cfg)
    clusters: list[ClusterConfig] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            logger.warning("跳过非法集群配置：%r", item)
            continue
        sources = ClusterSources(
            prometheus_url=item.get("prometheus_url", ""),
            elasticsearch_url=item.get("elasticsearch_url", ""),
            elasticsearch_index=item.get("elasticsearch_index", base.sources.elasticsearch_index),
            elasticsearch_username=item.get("elasticsearch_username", ""),
            elasticsearch_password=item.get("elasticsearch_password", ""),
            jaeger_url=item.get("jaeger_url", ""),
            kubernetes_api_url=item.get("kubernetes_api_url", ""),
            kubernetes_token=item.get("kubernetes_token", ""),
        )
        clusters.append(
            ClusterConfig(
                name=str(item["name"]),
                namespace=str(item.get("namespace", "default")),
                enabled=bool(item.get("enabled", True)),
                sources=sources,
            )
        )

    logger.info("加载多集群配置：%s", [c.name for c in clusters])
    return clusters or [base]
