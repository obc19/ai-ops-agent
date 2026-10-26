"""告警策略测试：聚合、升级、P0 冷却。"""

from __future__ import annotations

from datetime import timedelta

from app.alerting.policy import AlertPolicy
from app.models import AnomalyEvent, DiagnosisReport, TimeWindow
from app.utils.clock import utc_now


def _anomaly(severity: str = "P1", target: str = "svc-a") -> AnomalyEvent:
    end = utc_now()
    return AnomalyEvent(
        source="metric", cluster="c1", namespace="prod", target=target,
        metric="cpu_usage_percent", window=TimeWindow(start=end - timedelta(minutes=5), end=end),
        severity=severity, value=95, threshold=90, rule_id="cpu-critical",
        rule_name="CPU 超限", summary=f"{target} CPU 超限",
    )


def _report(anomaly: AnomalyEvent) -> DiagnosisReport:
    return DiagnosisReport(
        anomaly_id=anomaly.id, target=anomaly.target, root_cause="CPU 打满",
        suggestion=["扩容"], confidence=0.8,
    )


def test_first_alert_created_and_notified(settings):
    policy = AlertPolicy(settings=settings)
    anomaly = _anomaly()
    alert, notify = policy.register(anomaly, _report(anomaly))
    assert notify is True
    assert alert.count == 1


def test_same_group_aggregates_without_notify(settings):
    policy = AlertPolicy(settings=settings)
    anomaly = _anomaly(target="svc-agg")
    policy.register(anomaly, _report(anomaly))
    again, notify = policy.register(anomaly, _report(anomaly))
    assert notify is False
    assert again.count == 2


def test_p0_escalates_existing_alert(settings):
    policy = AlertPolicy(settings=settings)
    anomaly_p1 = _anomaly(severity="P1", target="svc-escalate")
    alert, _ = policy.register(anomaly_p1, _report(anomaly_p1))
    anomaly_p0 = _anomaly(severity="P0", target="svc-escalate")
    escalated, notify = policy.register(anomaly_p0, _report(anomaly_p0))
    assert notify is True
    assert escalated.id == alert.id
    assert escalated.severity.value == "P0"


def test_p0_within_cooldown_does_not_realert(settings):
    policy = AlertPolicy(settings=settings)
    anomaly = _anomaly(severity="P0", target="svc-cool")
    alert, first = policy.register(anomaly, _report(anomaly))
    _, second = policy.register(anomaly, _report(anomaly))
    assert first is True
    assert second is False
    fresh = policy.db.get_alert(alert.id)
    assert fresh is not None and fresh.count >= 2
