"""告警接口：列表、详情、确认、关闭、手工重发。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.alerting.notifier import Notifier
from app.api.deps import require_auth
from app.models.alert import Alert
from app.models.enums import AlertStatus
from app.storage import get_store
from app.utils.clock import utc_now

router = APIRouter(prefix="/alerts", tags=["alerts"])


class ConfirmRequest(BaseModel):
    confirmed_by: str = "operator"
    note: str = ""


class ResolveRequest(BaseModel):
    note: str = ""
    resolved_by: str = "operator"


@router.get("", summary="告警列表")
async def list_alerts(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: AlertStatus | None = None,
    severity: str | None = None,
    cluster: str | None = None,
    actor: str = Depends(require_auth),
) -> dict:
    items, total = get_store().list_alerts(
        limit=limit,
        offset=offset,
        status=status.value if status else None,
        severity=severity,
        cluster=cluster,
    )
    return {
        "total": total,
        "items": [item.model_dump(mode="json") for item in items],
        "severity_count": get_store().count_alerts_by_severity(),
    }


@router.get("/{alert_id}", response_model=Alert, summary="告警详情")
async def get_alert(alert_id: str, actor: str = Depends(require_auth)) -> Alert:
    alert = get_store().get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警不存在：{alert_id}")
    return alert


@router.get("/{alert_id}/markdown", response_class=PlainTextResponse, summary="告警 Markdown")
async def get_alert_markdown(alert_id: str, actor: str = Depends(require_auth)) -> PlainTextResponse:
    alert = get_store().get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警不存在：{alert_id}")
    return PlainTextResponse(alert.markdown(), media_type="text/markdown; charset=utf-8")


@router.post("/{alert_id}/confirm", response_model=Alert, summary="确认告警（进入处置）")
async def confirm_alert(
    alert_id: str, payload: ConfirmRequest, actor: str = Depends(require_auth)
) -> Alert:
    store = get_store()
    alert = store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警不存在：{alert_id}")
    alert.status = AlertStatus.CONFIRMED
    alert.confirmed_by = payload.confirmed_by
    alert.note = payload.note
    store.save_alert(alert)
    store.append_audit(
        "alert_confirmed",
        {"alert_id": alert_id, "note": payload.note},
        actor=payload.confirmed_by or actor,
        target=alert_id,
    )
    return alert


@router.post("/{alert_id}/resolve", response_model=Alert, summary="关闭告警")
async def resolve_alert(
    alert_id: str, payload: ResolveRequest, actor: str = Depends(require_auth)
) -> Alert:
    store = get_store()
    alert = store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警不存在：{alert_id}")
    alert.status = AlertStatus.RESOLVED
    alert.resolved_at = utc_now()
    alert.note = payload.note or alert.note
    store.save_alert(alert)
    store.append_audit(
        "alert_resolved",
        {"alert_id": alert_id, "note": payload.note},
        actor=payload.resolved_by or actor,
        target=alert_id,
    )
    return alert


@router.post("/{alert_id}/renotify", summary="重新推送告警（手工重发）")
async def renotify(alert_id: str, actor: str = Depends(require_auth)) -> dict:
    store = get_store()
    alert = store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"告警不存在：{alert_id}")
    results = await Notifier().dispatch(alert)
    alert.notifications = results
    store.save_alert(alert)
    store.append_audit(
        "alert_renotified", {"alert_id": alert_id}, actor=actor, target=alert_id
    )
    return {"alert_id": alert_id, "results": [r.model_dump(mode="json") for r in results]}
