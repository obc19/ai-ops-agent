"""API 路由汇总。"""

from fastapi import APIRouter

from app.api.routes import (
    alerts,
    audit,
    clusters,
    diagnostics,
    ingest,
    knowledge,
    overview,
    system,
)

api_router = APIRouter()
api_router.include_router(overview.router)
api_router.include_router(clusters.router)
api_router.include_router(diagnostics.router)
api_router.include_router(alerts.router)
api_router.include_router(knowledge.router)
api_router.include_router(audit.router)
api_router.include_router(ingest.router)
api_router.include_router(system.router)

__all__ = ["api_router"]
