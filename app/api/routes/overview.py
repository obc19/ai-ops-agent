"""总览接口：控制台首屏所需的聚合数据。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_auth
from app.collectors import collectors_health
from app.collectors.k8s_adapter import in_cluster as k8s_in_cluster
from app.config.clusters import load_clusters
from app.config.settings import get_settings
from app.knowledge.experience import get_knowledge_service
from app.mock import world
from app.storage import get_store

router = APIRouter(tags=["overview"])


@router.get("/overview", summary="全局总览")
async def overview(actor: str = Depends(require_auth)) -> dict:
    settings = get_settings()
    store = get_store()
    health = await collectors_health()

    return {
        "stats": store.overview(),
        "severity_count": store.count_alerts_by_severity(),
        "knowledge": get_knowledge_service().stats().model_dump(),
        "clusters": [
            {
                "name": c.name,
                "namespace": c.namespace,
                "enabled": c.enabled,
                "sources": c.sources.configured_sources,
            }
            for c in load_clusters(settings)
        ],
        "sources": [h.to_public() for h in health],
        "mode": {
            "mock": settings.mock_mode,
            "llm_enabled": settings.llm_enabled,
            "llm_model": settings.llm_model,
            "embedding_enabled": settings.embedding_enabled,
            "trace_agent": settings.enable_trace_agent,
            # 集群内跑时适配器自动启用，这里回报「实际是否在工作」而不是原始开关
            "k8s_adapter": settings.enable_k8s_adapter or k8s_in_cluster(),
            "k8s_in_cluster": k8s_in_cluster(),
            "knowledge_backend": settings.knowledge_backend,
            "detect_interval": settings.detect_interval,
            "auth_enabled": settings.auth_enabled,
            "clusters": len(load_clusters(settings)),
        },
        "mock_faults": [
            {"target": f.target, "metric": f.metric, "severity": f.severity}
            for f in world.list_faults()
        ],
    }
