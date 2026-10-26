"""诊断接口：手动触发、立即检测、查询报告。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.models.diagnosis import DiagnosisReport
from app.models.enums import Severity
from app.models.events import AnomalyEvent, TimeWindow
from app.services.pipeline import get_pipeline
from app.storage import get_store
from app.utils.clock import utc_now

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class DiagnosisRequest(BaseModel):
    """手工构造一次异常并触发诊断（也可用于告警系统 webhook 接入）。"""

    target: str = Field(description="服务 / Pod / 节点名")
    metric: str = Field(default="cpu_usage_percent")
    cluster: str = "prod-cluster"
    namespace: str = "prod"
    severity: Severity = Severity.P1
    value: float | None = None
    threshold: float | None = None
    rule_id: str = "manual"
    rule_name: str = "手工触发"
    summary: str = ""
    window_minutes: int = Field(default=5, ge=1, le=180)
    notify: bool = True


class DetectRequest(BaseModel):
    notify: bool = True


@router.post("", summary="触发一次诊断")
async def create_diagnosis(
    payload: DiagnosisRequest, actor: str = Depends(require_auth)
) -> dict:
    end = utc_now()
    anomaly = AnomalyEvent(
        source="metric",
        cluster=payload.cluster,
        namespace=payload.namespace,
        target=payload.target,
        metric=payload.metric,
        window=TimeWindow(start=end - timedelta(minutes=payload.window_minutes), end=end),
        severity=payload.severity,
        value=payload.value,
        threshold=payload.threshold,
        rule_id=payload.rule_id,
        rule_name=payload.rule_name,
        summary=payload.summary or f"{payload.target} 的 {payload.metric} 异常（手工触发）",
    )
    result = await get_pipeline().handle_anomaly(anomaly, notify=payload.notify)
    get_store().append_audit(
        "diagnosis_requested", {"anomaly_id": anomaly.id}, actor=actor, target=payload.target
    )
    return result.to_public()


@router.post("/detect", summary="立即执行一轮采集与检测")
async def detect_now(payload: DetectRequest, actor: str = Depends(require_auth)) -> dict:
    results = await get_pipeline().run_detection_once(notify=payload.notify)
    return {
        "count": len(results),
        "items": [
            {
                "report_id": r.report.id,
                "alert_id": r.alert.id,
                "target": r.report.target,
                "severity": r.alert.severity.value,
                "alert_is_new": r.alert_is_new,
                "notifications": [n.model_dump(mode="json") for n in r.notifications],
            }
            for r in results
        ],
    }


@router.get("", response_model=list[DiagnosisReport], summary="诊断记录列表")
async def list_diagnoses(
    limit: int = Query(default=20, ge=1, le=200),
    cluster: str | None = None,
    actor: str = Depends(require_auth),
) -> list[DiagnosisReport]:
    return get_store().list_diagnoses(limit=limit, cluster=cluster)


@router.get("/{diagnosis_id}", response_model=DiagnosisReport, summary="诊断详情")
async def get_diagnosis(
    diagnosis_id: str, actor: str = Depends(require_auth)
) -> DiagnosisReport:
    report = get_store().get_diagnosis(diagnosis_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"诊断报告不存在：{diagnosis_id}")
    return report


@router.get("/{diagnosis_id}/markdown", response_class=PlainTextResponse, summary="导出 Markdown")
async def get_diagnosis_markdown(
    diagnosis_id: str, actor: str = Depends(require_auth)
) -> PlainTextResponse:
    report = get_store().get_diagnosis(diagnosis_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"诊断报告不存在：{diagnosis_id}")
    return PlainTextResponse(report.markdown, media_type="text/markdown; charset=utf-8")
