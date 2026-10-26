"""异常检测测试：阈值、环比、基线、日志规则与去重。"""

from __future__ import annotations

from datetime import timedelta

from app.detection import get_engine
from app.models.events import LogEvent, MetricEvent, MetricSample, TimeWindow
from app.utils.clock import utc_now


def _window(minutes: int = 5) -> TimeWindow:
    end = utc_now()
    return TimeWindow(start=end - timedelta(minutes=minutes), end=end)


def _metric_event(metric: str, values: list[float], target: str = "svc-a") -> MetricEvent:
    w = _window()
    step = w.duration_seconds / max(len(values) - 1, 1)
    samples = [
        MetricSample(timestamp=w.start + timedelta(seconds=step * i), value=v)
        for i, v in enumerate(values)
    ]
    return MetricEvent(
        cluster="c1", namespace="prod", target=target, metric=metric, unit="%",
        value=values[-1], samples=samples,
    )


def test_static_threshold_triggers_p0():
    window = _window()
    events = [_metric_event("cpu_usage_percent", [30, 40, 60, 94])]
    anomalies = get_engine().detect(events, window)
    assert any(a.rule_id == "cpu-critical" and a.severity.value == "P0" for a in anomalies)


def test_dedupe_keeps_highest_severity():
    window = _window()
    events = [_metric_event("cpu_usage_percent", [20] * 4 + [95, 96])]
    anomalies = get_engine().detect(events, window)
    cpu = [a for a in anomalies if a.metric == "cpu_usage_percent"]
    assert len(cpu) == 1
    assert cpu[0].severity.value == "P0"


def test_baseline_detects_spike():
    window = _window()
    values = [10.0] * 8 + [12.0, 60.0]
    events = [_metric_event("http_latency_p99_ms", values)]
    anomalies = get_engine().detect(events, window)
    assert any(a.rule_id.startswith("baseline-") for a in anomalies)


def test_log_rule_matches_oom():
    window = _window()
    logs = [
        LogEvent(
            cluster="c1", namespace="prod", target="payment-service",
            level="ERROR", message="java.lang.OutOfMemoryError: Java heap space", count=9,
        )
    ]
    anomalies = get_engine().detect(logs, window)
    assert anomalies and anomalies[0].rule_id == "log-oom"
    assert anomalies[0].severity.value == "P1"


def test_normal_metrics_no_anomaly():
    window = _window()
    events = [_metric_event("cpu_usage_percent", [30, 31, 33, 32, 30])]
    assert get_engine().detect(events, window) == []
