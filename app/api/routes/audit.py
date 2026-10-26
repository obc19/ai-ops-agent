"""审计日志接口（含数据外发记录）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_auth
from app.storage import get_store

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", summary="审计日志查询")
async def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    egress_only: bool = Query(default=False, description="只看数据外发记录"),
    actor: str = Depends(require_auth),
) -> dict:
    items = get_store().list_audit(limit=limit, egress_only=egress_only)
    return {"total": len(items), "items": items, "egress_only": egress_only}
