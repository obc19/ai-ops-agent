"""诊断闭环流水线：异常 → 诊断 → 告警 → 通知，供 API 与调度器共用。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.orchestrator import Orchestrator
from app.alerting.notifier import Notifier
from app.alerting.policy import AlertPolicy
from app.config.settings import get_settings
from app.models.alert import Alert, NotificationResult
from app.models.diagnosis import DiagnosisReport
from app.models.events import AnomalyEvent
from app.storage import get_store
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PipelineResult:
    report: DiagnosisReport
    alert: Alert
    notifications: list[NotificationResult] = field(default_factory=list)
    alert_is_new: bool = True

    def to_public(self) -> dict:
        return {
            "report": self.report.model_dump(mode="json"),
            "alert": self.alert.model_dump(mode="json"),
            "notifications": [n.model_dump(mode="json") for n in self.notifications],
            "alert_is_new": self.alert_is_new,
        }


class DiagnosisPipeline:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = get_store()
        self.orchestrator = Orchestrator(self.db)
        self.policy = AlertPolicy(self.settings, self.db)
        self.notifier = Notifier(self.settings)

    async def handle_anomaly(
        self, anomaly: AnomalyEvent, notify: bool = True
    ) -> PipelineResult:
        report = await self.orchestrator.run(anomaly)
        alert, is_new = self.policy.register(anomaly, report)

        notifications: list[NotificationResult] = []
        if notify and is_new:
            notifications = await self.notifier.dispatch(alert)
            alert.notifications = notifications
            self.db.save_alert(alert)

        return PipelineResult(
            report=report, alert=alert, notifications=notifications, alert_is_new=is_new
        )

    async def run_detection_once(self, notify: bool = True) -> list[PipelineResult]:
        """一轮「采集 + 检测 + 逐个诊断」，调度器与 /diagnostics/detect 共用。"""
        from datetime import timedelta

        from app.collectors import collect_all
        from app.detection import get_engine
        from app.models.events import TimeWindow
        from app.utils.clock import utc_now

        window = TimeWindow(start=utc_now() - timedelta(minutes=5), end=utc_now())
        events = await collect_all(window)
        anomalies = get_engine().detect(events, window)
        if not anomalies:
            logger.debug("本轮检测未发现异常")
            return []

        results: list[PipelineResult] = []
        for anomaly in anomalies:
            try:
                results.append(await self.handle_anomaly(anomaly, notify=notify))
            except Exception as exc:  # noqa: BLE001 - 单条异常失败不影响其他
                logger.exception("处理异常 %s 失败：%s", anomaly.id, exc)
        return results


_pipeline: DiagnosisPipeline | None = None


def get_pipeline() -> DiagnosisPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = DiagnosisPipeline()
    return _pipeline
