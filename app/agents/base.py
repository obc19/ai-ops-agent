"""Agent 基类与诊断上下文。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.diagnosis import DiagnosisFragment, RAGHit
from app.models.enums import AgentKind
from app.models.events import (
    AnomalyEvent,
    K8sEvent,
    LogEvent,
    MetricEvent,
    TimeWindow,
    TraceEvent,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DiagnosisContext:
    """一次诊断的共享上下文，Agent 只读。"""

    anomaly: AnomalyEvent
    window: TimeWindow
    metric_events: list[MetricEvent] = field(default_factory=list)
    log_events: list[LogEvent] = field(default_factory=list)
    trace_events: list[TraceEvent] = field(default_factory=list)
    k8s_events: list[K8sEvent] = field(default_factory=list)
    rag_hits: list[RAGHit] = field(default_factory=list)
    store: Any = None
    # 该对象在服务依赖图中的位置（orchestrator 预取），供因果推断使用
    topology: dict = field(default_factory=dict)

    @property
    def target(self) -> str:
        return self.anomaly.target

    @property
    def metric(self) -> str:
        return self.anomaly.metric

    def metrics_for(self, metric: str | None = None) -> list[MetricEvent]:
        metric = metric or self.anomaly.metric
        return [
            e
            for e in self.metric_events
            if e.target == self.target and e.metric == metric
        ]

    def logs_for(self) -> list[LogEvent]:
        return [e for e in self.log_events if e.target == self.target] or self.log_events

    def traces_for(self) -> list[TraceEvent]:
        return [e for e in self.trace_events if e.target == self.target]

    def k8s_for(self) -> list[K8sEvent]:
        return [e for e in self.k8s_events if e.target == self.target]


class BaseAgent(ABC):
    """每个 Agent 只负责一个维度，必须给出置信度与依据。"""

    kind: AgentKind
    label: str = ""
    weight: float = 0.3

    @abstractmethod
    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:
        ...

    def unavailable(self, reason: str) -> DiagnosisFragment:
        """可选模块未启用：不参与诊断，也不计入降级惩罚。"""
        logger.info("%s 未启用：%s", self.label or self.kind.value, reason)
        return DiagnosisFragment(
            agent=self.kind,
            confidence=0.0,
            conclusion="未启用（可选模块）",
            degraded=False,
            skip_reason=reason,
        )

    def skip(self, reason: str) -> DiagnosisFragment:
        logger.info("%s 跳过：%s", self.label or self.kind.value, reason)
        return DiagnosisFragment(
            agent=self.kind,
            confidence=0.0,
            conclusion="未参与本次诊断",
            degraded=True,
            skip_reason=reason,
        )
