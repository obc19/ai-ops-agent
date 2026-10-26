"""外部告警接入口：Alertmanager Webhook → 统一异常事件 → 诊断闭环。

用于已有 Prometheus + Alertmanager 体系的环境：不必让本工具重复做阈值判断，
直接把已有告警作为异常来源接入，后续诊断、根因、分级告警、经验沉淀完全复用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import require_operator
from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, TimeWindow
from app.services.pipeline import get_pipeline
from app.utils.clock import utc_now
from app.utils.logging import get_logger

router = APIRouter(prefix="/ingest", tags=["ingest"])
logger = get_logger(__name__)

SEVERITY_MAP = {
    "critical": Severity.P0,
    "fatal": Severity.P0,
    "emergency": Severity.P0,
    "warning": Severity.P1,
    "warn": Severity.P1,
    "info": Severity.P2,
    "notice": Severity.P2,
}


class AlertmanagerAlert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str = ""


class AlertmanagerPayload(BaseModel):
    """Alertmanager v4 webhook 结构（多余字段忽略）。"""

    version: str = "4"
    status: str = "firing"
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def to_anomaly(item: AlertmanagerAlert, common: dict[str, str]) -> AnomalyEvent:
    labels = {**common, **item.labels}
    alertname = labels.get("alertname") or item.fingerprint or "external_alert"
    severity = SEVERITY_MAP.get(labels.get("severity", "").lower(), Severity.P1)
    now = utc_now()
    start = _parse_time(item.startsAt) or now - timedelta(minutes=5)
    summary = (
        item.annotations.get("summary")
        or item.annotations.get("description")
        or f"{alertname} 触发"
    )
    return AnomalyEvent(
        source=SourceType.METRIC,
        cluster=labels.get("cluster", "default"),
        namespace=labels.get("namespace", "default"),
        target=labels.get("instance") or labels.get("pod") or labels.get("job") or alertname,
        metric=alertname,
        window=TimeWindow(start=start, end=now if now > start else start),
        severity=severity,
        rule_id=f"alertmanager:{alertname}",
        rule_name=alertname,
        summary=summary[:300],
        context={
            "origin": "alertmanager",
            "labels": labels,
            "annotations": item.annotations,
            "fingerprint": item.fingerprint,
        },
    )


@router.post("/alertmanager", summary="接收 Alertmanager Webhook 并进入诊断闭环")
async def ingest_alertmanager(
    payload: AlertmanagerPayload,
    notify: bool = Query(default=True, description="是否推送分级告警"),
    ignore_resolved: bool = Query(default=True, description="是否忽略已恢复的告警"),
    actor: str = Depends(require_operator),
) -> dict:
    pipeline = get_pipeline()
    common = payload.commonLabels
    items: list[dict] = []
    skipped = 0

    for item in payload.alerts:
        if ignore_resolved and item.status.lower() == "resolved":
            skipped += 1
            continue
        anomaly = to_anomaly(item, common)
        try:
            result = await pipeline.handle_anomaly(anomaly, notify=notify)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
            logger.exception("接入告警 %s 处理失败：%s", anomaly.metric, exc)
            items.append(
                {
                    "anomaly_id": anomaly.id,
                    "metric": anomaly.metric,
                    "target": anomaly.target,
                    "error": str(exc)[:300],
                }
            )
            continue
        items.append(
            {
                "anomaly_id": anomaly.id,
                "severity": anomaly.severity.value,
                "target": anomaly.target,
                "metric": anomaly.metric,
                "report_id": result.report.id,
                "alert_id": result.alert.id,
                "alert_status": result.alert.status.value,
                "alert_is_new": result.alert_is_new,
                "notifications": [
                    {"channel": n.channel, "ok": n.ok} for n in result.notifications
                ],
            }
        )

    logger.info("接入 Alertmanager 告警 %d 条（跳过 %d 条）", len(items), skipped)
    return {"accepted": len(items), "skipped": skipped, "items": items}
