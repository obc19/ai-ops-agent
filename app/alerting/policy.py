"""告警策略：分级、聚合去重、P0 升级、通知冷却。

规则说明（与确认书 D13 的实现口径）：
- 同一 group_key（集群/命名空间/对象/指标）在聚合窗口内已存在未关闭告警时合并计数；
- P0 不参与「静默抑制」：要么把已有告警升级为 P0 并立即通知，要么在冷却期外再次通知；
- 冷却期内仍聚合计数，避免轮询周期（默认 10s）造成的告警风暴。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config.settings import Settings, get_settings
from app.models.alert import Alert, StatusChange
from app.models.diagnosis import DiagnosisReport
from app.models.enums import AlertStatus, Severity
from app.models.events import AnomalyEvent, TimeWindow
from app.storage import get_store
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)


class AlertPolicy:
    def __init__(self, settings: Settings | None = None, store=None) -> None:
        self.settings = settings or get_settings()
        self.db = store or get_store()

    def register(
        self, anomaly: AnomalyEvent, report: DiagnosisReport | None
    ) -> tuple[Alert, bool]:
        """返回 (告警, 是否需要通知)。"""
        group_key = anomaly.group_key()
        window_start = utc_now() - timedelta(seconds=self.settings.alert_group_window)
        existing = self.db.find_alert_in_group(group_key, window_start)

        if existing is None:
            return self._create(anomaly, report, group_key), True

        self._apply_report(existing, anomaly, report)

        # 升级：新异常级别更高 → 提级并立即通知
        if anomaly.severity.rank < existing.severity.rank:
            logger.info(
                "告警升级：%s %s → %s", group_key, existing.severity.value, anomaly.severity.value
            )
            existing.severity = anomaly.severity
            existing.mark_seen()
            self.db.save_alert(existing)
            return existing, True

        # P0 冷却期外再次通知；其余级别一律聚合
        if anomaly.severity is Severity.P0 and not self._in_cooldown(existing):
            existing.mark_seen()
            self.db.save_alert(existing)
            logger.info("P0 告警 %s 重新通知（已过冷却期）", existing.id)
            return existing, True

        existing.mark_seen()
        self.db.save_alert(existing)
        logger.info("告警聚合：%s 已存在（第 %d 次），不重复通知", group_key, existing.count)
        return existing, False

    # ---------- 内部 ----------
    def _create(
        self, anomaly: AnomalyEvent, report: DiagnosisReport | None, group_key: str
    ) -> Alert:
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
        alert.status_history.append(
            StatusChange(to_status=AlertStatus.TRIGGERED, actor="system", note="检测到异常")
        )
        self.db.save_alert(alert)
        logger.info("生成告警 %s（%s）：%s", alert.id, alert.severity.value, alert.summary)
        return alert

    @staticmethod
    def _apply_report(
        alert: Alert, anomaly: AnomalyEvent, report: DiagnosisReport | None
    ) -> None:
        if report is not None:
            alert.root_cause = report.root_cause or alert.root_cause
            alert.suggestion = report.suggestion or alert.suggestion
            alert.diagnosis_id = report.id or alert.diagnosis_id
            alert.degraded = report.status.value != "ok"
        alert.summary = anomaly.summary or alert.summary

    def _in_cooldown(self, alert: Alert) -> bool:
        if self.settings.alert_cooldown <= 0:
            return False
        last: datetime = alert.created_at
        for result in alert.notifications:
            if result.sent_at and result.sent_at > last:
                last = result.sent_at
        elapsed = (utc_now() - last).total_seconds()
        return elapsed < self.settings.alert_cooldown

    def summarize(self, alert: Alert) -> str:
        state = "已关闭" if alert.status is AlertStatus.RESOLVED else "处理中"
        return f"[{alert.severity}] {alert.target} {alert.metric} ×{alert.count}（{state}）"


def raise_system_alert(
    metric: str,
    summary: str,
    *,
    severity: Severity = Severity.P1,
    detail: str = "",
    suggestion: list[str] | None = None,
    store=None,
) -> Alert:
    """自监控告警：工具自身异常（如脱敏失败）也要进告警列表，不能只写日志。"""
    db = store or get_store()
    now = utc_now()
    alert = Alert(
        severity=severity,
        cluster="system",
        namespace="ai-ops-agent",
        target="safety-gateway",
        metric=metric,
        window=TimeWindow(start=now, end=now),
        summary=summary,
        root_cause=detail or summary,
        suggestion=suggestion or ["检查脱敏规则配置", "确认外发被拦截的请求内容后重试"],
        group_key=f"system/ai-ops-agent/{metric}",
        channels=[],
    )
    alert.status_history.append(
        StatusChange(to_status=AlertStatus.TRIGGERED, actor="system", note="工具自检")
    )
    db.save_alert(alert)
    logger.error("自监控告警 [%s] %s", severity.value, summary)
    return alert
