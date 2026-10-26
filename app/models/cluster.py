"""集群模型：多集群 / 多命名空间从数据模型起就贯穿。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.utils.clock import utc_now
from app.utils.ids import new_id


class ClusterRegistration(BaseModel):
    name: str
    namespaces: list[str] = Field(default_factory=lambda: ["default"])
    prometheus_url: str = ""
    elasticsearch_url: str = ""
    jaeger_url: str = ""
    kubernetes_api_url: str = ""
    note: str = ""


class Cluster(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cls"))
    name: str
    namespaces: list[str] = Field(default_factory=lambda: ["default"])
    prometheus_url: str = ""
    elasticsearch_url: str = ""
    jaeger_url: str = ""
    kubernetes_api_url: str = ""
    note: str = ""
    healthy: bool = True
    detail: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)


class ClusterHealth(BaseModel):
    cluster: str
    healthy: bool
    sources: list[dict] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)
