"""诊断闭环流水线：异常 → 诊断 → 告警 → 通知，供 API 与调度器共用。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.orchestrator import Orchestrator
from app.alerting.notifier import Notifier
from app.alerting.policy import AlertPolicy
from app.config.settings import get_settings
from app.models.alert import Alert, NotificationResult
from app.models.enums import AlertStatus
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
    # 同源收敛信息：根因 + 被抑制的异常 + 判定理由
    correlation: dict = field(default_factory=dict)

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
        self, anomaly: AnomalyEvent, notify: bool = True, correlation=None
    ) -> PipelineResult:
        report = await self.orchestrator.run(anomaly, correlation=correlation)
        alert, is_new = self.policy.register(anomaly, report)

        notifications: list[NotificationResult] = []
        if notify and is_new:
            notifications = await self.notifier.dispatch(alert)
            alert.notifications = notifications
            if any(n.ok for n in notifications) and alert.status is AlertStatus.TRIGGERED:
                channels = ",".join(n.channel for n in notifications if n.ok)
                alert.transition(AlertStatus.NOTIFIED, actor="system", note=f"已通知：{channels}")
            self.db.save_alert(alert)

        return PipelineResult(
            report=report,
            alert=alert,
            notifications=notifications,
            alert_is_new=is_new,
            correlation=correlation.to_public() if correlation is not None else {},
        )

    async def run_detection_once(self, notify: bool = True) -> list[PipelineResult]:
        """一轮「采集 + 检测 + 关联收敛 + 逐组诊断」，调度器与 /diagnostics/detect 共用。

        关键点：**先收敛再诊断**。同一时刻的多条异常若来自同一条调用链，
        只对最上游的根因做完整诊断并发出告警，其余作为「同源受影响」附着在
        报告里，不再各自成案 —— 这是把 N 条告警压成 1 条的地方。
        """
        from datetime import timedelta

        from app.collectors import collect_all
        from app.detection import get_engine
        from app.models.events import TimeWindow
        from app.topology import correlate, get_topology
        from app.utils.clock import utc_now

        window = TimeWindow(start=utc_now() - timedelta(minutes=5), end=utc_now())
        events = await collect_all(window)
        anomalies = get_engine().detect(events, window)
        if not anomalies:
            logger.debug("本轮检测未发现异常")
            return []

        graph = await get_topology().current()
        clusters = correlate(
            anomalies, graph, escalate_by_impact=self.settings.topology_escalate_by_impact
        )
        if len(clusters) < len(anomalies):
            logger.info(
                "检测到 %d 条异常，经拓扑收敛为 %d 组（抑制 %d 条重复告警）",
                len(anomalies),
                len(clusters),
                len(anomalies) - len(clusters),
            )

        results: list[PipelineResult] = []
        for cluster in clusters:
            try:
                results.append(
                    await self.handle_anomaly(cluster.root, notify=notify, correlation=cluster)
                )
            except Exception as exc:  # noqa: BLE001 - 单条异常失败不影响其他
                logger.exception("处理异常 %s 失败：%s", cluster.root.id, exc)
        return results


_pipeline: DiagnosisPipeline | None = None


def get_pipeline() -> DiagnosisPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = DiagnosisPipeline()
    return _pipeline
