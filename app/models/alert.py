"""告警模型：分级、聚合计数、状态机与通知结果。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import AlertStatus, Severity
from app.models.events import TimeWindow
from app.utils.clock import utc_now, utc_now as _now
from app.utils.ids import new_id


class NotificationResult(BaseModel):
    channel: str
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0
    sent_at: datetime = Field(default_factory=utc_now)


class InvalidTransition(RuntimeError):
    """非法的状态流转。"""

    def __init__(self, current: AlertStatus, target: AlertStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"告警状态不允许从 {current.value} 流转到 {target.value}")


class StatusChange(BaseModel):
    """状态流转记录，用于回溯告警处置过程。"""

    from_status: AlertStatus | None = None
    to_status: AlertStatus
    actor: str = "system"
    note: str = ""
    at: datetime = Field(default_factory=utc_now)


# 触发 → 通知 → 确认（处置中）→ 关闭；允许误报直接关闭、复发热重开、升级后重发
ALLOWED_TRANSITIONS: dict[AlertStatus, set[AlertStatus]] = {
    AlertStatus.TRIGGERED: {AlertStatus.NOTIFIED, AlertStatus.CONFIRMED, AlertStatus.RESOLVED},
    AlertStatus.NOTIFIED: {AlertStatus.CONFIRMED, AlertStatus.RESOLVED, AlertStatus.NOTIFIED},
    AlertStatus.CONFIRMED: {AlertStatus.RESOLVED, AlertStatus.NOTIFIED},
    AlertStatus.RESOLVED: {AlertStatus.TRIGGERED},
}

STATUS_LABELS: dict[AlertStatus, str] = {
    AlertStatus.TRIGGERED: "已触发",
    AlertStatus.NOTIFIED: "已通知",
    AlertStatus.CONFIRMED: "处置中",
    AlertStatus.RESOLVED: "已关闭",
}


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: new_id("alert"))
    severity: Severity = Severity.P1
    status: AlertStatus = AlertStatus.TRIGGERED
    cluster: str = "default"
    namespace: str = "default"
    target: str = ""
    metric: str = ""
    window: TimeWindow
    summary: str = ""
    root_cause: str = ""
    suggestion: list[str] = Field(default_factory=list)
    diagnosis_id: str = ""
    anomaly_id: str = ""
    group_key: str = ""
    count: int = 1
    degraded: bool = False
    channels: list[str] = Field(default_factory=list)
    notifications: list[NotificationResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=_now)
    confirmed_by: str = ""
    resolved_at: datetime | None = None
    notified_at: datetime | None = None
    status_history: list[StatusChange] = Field(default_factory=list)
    note: str = ""

    def mark_seen(self) -> None:
        self.count += 1
        self.last_seen_at = utc_now()

    def transition(
        self, target: AlertStatus, actor: str = "system", note: str = ""
    ) -> "Alert":
        """按状态机流转；非法流转抛 InvalidTransition，由调用方转成 409。"""
        if target is self.status:
            return self
        if target not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise InvalidTransition(self.status, target)
        self.status_history.append(
            StatusChange(from_status=self.status, to_status=target, actor=actor, note=note)
        )
        self.status = target
        if target is AlertStatus.RESOLVED:
            self.resolved_at = utc_now()
        elif target is AlertStatus.CONFIRMED:
            self.confirmed_by = self.confirmed_by or actor
            self.resolved_at = None
        elif target is AlertStatus.NOTIFIED:
            self.notified_at = utc_now()
        elif target is AlertStatus.TRIGGERED:
            self.resolved_at = None
            self.confirmed_by = ""
        return self

    def markdown(self) -> str:
        from app.utils.clock import fmt_local

        lines = [
            f"### 【{self.severity}】{self.summary or '检测到异常'}",
            "",
            f"- **对象**：{self.cluster} / {self.namespace} / {self.target}",
            f"- **指标**：{self.metric}",
            f"- **时间**：{fmt_local(self.window.start)} ~ {fmt_local(self.window.end)}",
            f"- **根因判断**：{self.root_cause or '待诊断'}",
            f"- **状态**：{STATUS_LABELS.get(self.status, self.status.value)}（第 {self.count} 次）",
        ]
        if self.degraded:
            lines.append("- **注意**：本次诊断部分维度降级，结论仅供参考")
        if self.suggestion:
            lines.append("- **处置建议**：")
            lines.extend(f"  {idx}. {step}" for idx, step in enumerate(self.suggestion, 1))
        return "\n".join(lines)
