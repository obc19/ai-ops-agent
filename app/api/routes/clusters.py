"""集群接入与健康检查。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_auth
from app.collectors import collectors_health
from app.models.cluster import Cluster, ClusterRegistration
from app.storage import get_store
from app.utils.clock import utc_now

router = APIRouter(prefix="/clusters", tags=["clusters"])


@router.post("", response_model=Cluster, summary="注册/更新集群")
async def register_cluster(
    payload: ClusterRegistration, actor: str = Depends(require_auth)
) -> Cluster:
    store = get_store()
    existing = store.get_cluster_by_name(payload.name)
    cluster = Cluster(
        id=existing.id if existing else Cluster(name=payload.name).id,
        **payload.model_dump(),
    )
    store.save_cluster(cluster)
    store.append_audit(
        "cluster_registered", {"cluster": cluster.name}, actor=actor, target=cluster.name
    )
    return cluster


@router.get("", response_model=list[Cluster], summary="集群列表")
async def list_clusters(actor: str = Depends(require_auth)) -> list[Cluster]:
    return get_store().list_clusters()


@router.get("/{name}/health", summary="集群数据源健康度")
async def cluster_health(name: str, actor: str = Depends(require_auth)) -> dict:
    store = get_store()
    cluster = store.get_cluster_by_name(name)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"集群不存在：{name}")
    healths = await collectors_health()
    healthy = all(h.reachable for h in healths if h.configured) if healths else False
    cluster.healthy = healthy
    cluster.last_seen_at = utc_now()
    store.save_cluster(cluster)
    return {
        "cluster": name,
        "healthy": healthy,
        "sources": [h.to_public() for h in healths],
        "checked_at": cluster.last_seen_at.isoformat(),
    }
