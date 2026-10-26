"""事件模型：采集适配器的标准化输出 + 异常事件。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Severity, SourceType
from app.utils.clock import iso_utc, utc_now
from app.utils.ids import new_id


class TimeWindow(BaseModel):
    """统一时间窗口，存储 UTC。"""

    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return max((self.end - self.start).total_seconds(), 0.0)

    def label(self) -> str:
        from app.utils.clock import fmt_local

        return f"{fmt_local(self.start)} ~ {fmt_local(self.end)}"


class MetricSample(BaseModel):
    timestamp: datetime
    value: float


class BaseEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: new_id("evt"))
    source: SourceType
    cluster: str = "default"
    namespace: str = "default"
    target: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    def fingerprint(self) -> str:
        return f"{self.source}:{self.cluster}:{self.namespace}:{self.target}"


class MetricEvent(BaseEvent):
    source: SourceType = SourceType.METRIC
    metric: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    unit: str = ""
    value: float | None = None
    samples: list[MetricSample] = Field(default_factory=list)


class LogEvent(BaseEvent):
    source: SourceType = SourceType.LOG
    index: str = ""
    level: str = "ERROR"
    message: str = ""
    count: int = 1
    pattern: str = ""


class TraceEvent(BaseEvent):
    source: SourceType = SourceType.TRACE
    service: str = ""
    operation: str = ""
    trace_id: str = ""
    duration_ms: float = 0.0
    span_count: int = 1
    error: bool = False


class K8sEvent(BaseEvent):
    source: SourceType = SourceType.K8S
    reason: str = ""
    kind: str = ""
    message: str = ""
    count: int = 1


class AnomalyEvent(BaseModel):
    """检测模块产出的统一异常事件，是编排中枢的唯一入口。"""

    id: str = Field(default_factory=lambda: new_id("ano"))
    source: SourceType
    cluster: str = "default"
    namespace: str = "default"
    target: str = ""
    metric: str = ""
    window: TimeWindow
    severity: Severity = Severity.P1
    value: float | None = None
    threshold: float | None = None
    rule_id: str = ""
    rule_name: str = ""
    summary: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    def group_key(self) -> str:
        """告警聚合键：同对象 + 同指标。"""
        return f"{self.cluster}/{self.namespace}/{self.target}/{self.metric}"

    def display(self) -> str:
        return f"[{self.severity}] {self.cluster}/{self.namespace} {self.target} {self.metric}"


class DataSourceHealth(BaseModel):
    name: str
    configured: bool
    reachable: bool = False
    detail: str = ""
    checked_at: datetime = Field(default_factory=utc_now)

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "reachable": self.reachable,
            "detail": self.detail,
            "checked_at": iso_utc(self.checked_at),
        }
