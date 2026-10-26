"""阈值检测：静态阈值 + 百分比环比。"""

from __future__ import annotations

import operator
from typing import Any, Callable

from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, MetricEvent, TimeWindow

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
}


def _match(rule: dict[str, Any], event: MetricEvent) -> bool:
    return rule.get("metric") == event.metric


def detect_static(
    events: list[MetricEvent], rules: list[dict[str, Any]], window: TimeWindow
) -> list[AnomalyEvent]:
    anomalies: list[AnomalyEvent] = []
    for event in events:
        if not event.samples:
            continue
        current = event.samples[-1].value
        for rule in rules:
            if not _match(rule, event):
                continue
            op = _OPS.get(str(rule.get("operator", ">=")))
            threshold = float(rule.get("value", 0))
            if op is None or not op(current, threshold):
                continue
            anomalies.append(
                AnomalyEvent(
                    source=SourceType.METRIC,
                    cluster=event.cluster,
                    namespace=event.namespace,
                    target=event.target,
                    metric=event.metric,
                    window=window,
                    severity=Severity(rule.get("severity", "P1")),
                    value=round(current, 2),
                    threshold=threshold,
                    rule_id=str(rule.get("id", "")),
                    rule_name=str(rule.get("name", "")),
                    summary=(
                        f"{event.target} 的 {event.metric} 为 {current:.1f}"
                        f"{event.unit}，{rule.get('name', '超过阈值')}"
                        f"（阈值 {threshold:g}{event.unit}）"
                    ),
                    context={"operator": rule.get("operator"), "samples": len(event.samples)},
                )
            )
    return anomalies


def detect_percent_change(
    events: list[MetricEvent], rules: list[dict[str, Any]], window: TimeWindow
) -> list[AnomalyEvent]:
    anomalies: list[AnomalyEvent] = []
    for event in events:
        if len(event.samples) < 3:
            continue
        first = event.samples[0].value
        last = event.samples[-1].value
        if first <= 0:
            continue
        change = (last - first) / first * 100
        for rule in rules:
            if not _match(rule, event):
                continue
            limit = float(rule.get("percent", 50))
            if change < limit:
                continue
            anomalies.append(
                AnomalyEvent(
                    source=SourceType.METRIC,
                    cluster=event.cluster,
                    namespace=event.namespace,
                    target=event.target,
                    metric=event.metric,
                    window=window,
                    severity=Severity(rule.get("severity", "P2")),
                    value=round(last, 2),
                    threshold=round(first * (1 + limit / 100), 2),
                    rule_id=str(rule.get("id", "")),
                    rule_name=str(rule.get("name", "")),
                    summary=(
                        f"{event.target} 的 {event.metric} 在窗口内上升 {change:.1f}%"
                        f"（{first:.1f} → {last:.1f}），触发环比规则"
                    ),
                    context={"percent": round(change, 2), "baseline_start": first},
                )
            )
    return anomalies
