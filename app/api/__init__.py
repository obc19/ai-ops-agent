"""API 路由汇总。"""

from fastapi import APIRouter

from app.api.routes import (
    alerts,
    audit,
    cluster,
    clusters,
    diagnostics,
    host,
    ingest,
    knowledge,
    overview,
    system,
    topology,
    workflows,
)

api_router = APIRouter()
api_router.include_router(overview.router)
api_router.include_router(host.router)
api_router.include_router(cluster.router)
api_router.include_router(workflows.router)
api_router.include_router(topology.router)
api_router.include_router(clusters.router)
api_router.include_router(diagnostics.router)
api_router.include_router(alerts.router)
api_router.include_router(knowledge.router)
api_router.include_router(audit.router)
api_router.include_router(ingest.router)
api_router.include_router(system.router)

__all__ = ["api_router"]
