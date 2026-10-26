"""告警策略：分级、聚合去重、P0 不抑制、冷却窗口。"""

from __future__ import annotations

from datetime import timedelta

from app.config.settings import Settings, get_settings
from app.models.alert import Alert
from app.models.diagnosis import DiagnosisReport
from app.models.enums import Severity
from app.models.events import AnomalyEvent
from app.storage import get_store
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)


class AlertPolicy:
    """把「异常 + 诊断报告」转成告警，并决定是否复用已有告警。"""

    def __init__(self, settings: Settings | None = None, store=None) -> None:
        self.settings = settings or get_settings()
        self.db = store or get_store()

    def register(
        self, anomaly: AnomalyEvent, report: DiagnosisReport | None
    ) -> tuple[Alert, bool]:
        """返回 (告警, 是否新建)。P0 永远新建，其余在聚合窗口内合并。"""
        group_key = anomaly.group_key()
        window_start = utc_now() - timedelta(seconds=self.settings.alert_group_window)
        existing = self.db.find_alert_in_group(group_key, window_start)

        if existing is not None and anomaly.severity.suppressible:
            existing.mark_seen()
            if report is not None:
                existing.root_cause = report.root_cause or existing.root_cause
                existing.suggestion = report.suggestion or existing.suggestion
                existing.diagnosis_id = report.id or existing.diagnosis_id
                existing.degraded = report.status.value != "ok"
            existing.summary = anomaly.summary or existing.summary
            self.db.save_alert(existing)
            logger.info(
                "告警聚合：%s 已存在（第 %d 次），不重复通知", group_key, existing.count
            )
            return existing, False

        alert = Alert(
            severity=anomaly.severity,
            cluster=anomaly.cluster,
            namespace=anomaly.namespace,
            target=anomaly.target,
            metric=anomaly.metric,
            window=anomaly.window,
            summary=anomaly.summary,
            root_cause=report.root_cause if report else "",
            suggestion=report.suggestion if report else [],
            diagnosis_id=report.id if report else "",
            anomaly_id=anomaly.id,
            group_key=group_key,
            degraded=bool(report and report.status.value != "ok"),
            channels=[c.strip() for c in self.settings.alert_channels.split(",") if c.strip()],
        )
        self.db.save_alert(alert)
        reason = "P0 不参与抑制" if anomaly.severity is Severity.P0 else "新告警"
        logger.info("生成告警 %s（%s）：%s", alert.id, reason, alert.summary)
        return alert, True
