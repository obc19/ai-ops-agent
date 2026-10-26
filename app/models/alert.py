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
    note: str = ""

    def mark_seen(self) -> None:
        self.count += 1
        self.last_seen_at = utc_now()

    def markdown(self) -> str:
        from app.utils.clock import fmt_local

        lines = [
            f"### 【{self.severity}】{self.summary or '检测到异常'}",
            "",
            f"- **对象**：{self.cluster} / {self.namespace} / {self.target}",
            f"- **指标**：{self.metric}",
            f"- **时间**：{fmt_local(self.window.start)} ~ {fmt_local(self.window.end)}",
            f"- **根因判断**：{self.root_cause or '待诊断'}",
            f"- **状态**：{self.status}（第 {self.count} 次）",
        ]
        if self.degraded:
            lines.append("- **注意**：本次诊断部分维度降级，结论仅供参考")
        if self.suggestion:
            lines.append("- **处置建议**：")
            lines.extend(f"  {idx}. {step}" for idx, step in enumerate(self.suggestion, 1))
        return "\n".join(lines)
