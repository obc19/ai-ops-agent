"""枚举定义：与需求文档中的分级、状态一一对应。"""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    K8S = "k8s"


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"

    @property
    def rank(self) -> int:
        return {"P0": 0, "P1": 1, "P2": 2}[self.value]

    @property
    def suppressible(self) -> bool:
        """P0 不参与抑制，直接升级。"""
        return self is not Severity.P0


class AgentKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    ROOT_CAUSE = "root_cause"


class DiagnosisStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class AlertStatus(StrEnum):
    TRIGGERED = "triggered"
    CONFIRMED = "confirmed"
    RESOLVED = "resolved"


class ExperienceStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    ROLLED_BACK = "rolled_back"


class ChannelType(StrEnum):
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    EMAIL = "email"
    WEBHOOK = "webhook"
