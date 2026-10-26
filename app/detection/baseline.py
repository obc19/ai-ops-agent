"""基线检测：滑动窗口均值 / 标准差，3σ 判异。"""

from __future__ import annotations

import statistics
from typing import Any

from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, MetricEvent, TimeWindow


def detect_baseline(
    events: list[MetricEvent], config: dict[str, Any], window: TimeWindow
) -> list[AnomalyEvent]:
    sigma = float(config.get("sigma", 3.0))
    min_samples = int(config.get("min_samples", 6))
    train_ratio = float(config.get("train_ratio", 0.6))
    severity = Severity(config.get("severity", "P2"))

    anomalies: list[AnomalyEvent] = []
    for event in events:
        values = [s.value for s in event.samples]
        if len(values) < min_samples:
            continue
        split = max(int(len(values) * train_ratio), min_samples // 2)
        train, recent = values[:split], values[split:]
        if len(train) < 2 or not recent:
            continue
        mean = statistics.fmean(train)
        stdev = statistics.pstdev(train) or 0.0
        upper = mean + sigma * max(stdev, mean * 0.02)
        peak = max(recent)
        if peak <= upper:
            continue
        anomalies.append(
            AnomalyEvent(
                source=SourceType.METRIC,
                cluster=event.cluster,
                namespace=event.namespace,
                target=event.target,
                metric=event.metric,
                window=window,
                severity=severity,
                value=round(peak, 2),
                threshold=round(upper, 2),
                rule_id=f"baseline-{event.metric}",
                rule_name="基线偏离",
                summary=(
                    f"{event.target} 的 {event.metric} 偏离基线：峰值 {peak:.1f}，"
                    f"基线均值 {mean:.1f}，{sigma:g}σ 上界 {upper:.1f}"
                ),
                context={
                    "mean": round(mean, 2),
                    "stdev": round(stdev, 2),
                    "sigma": sigma,
                    "train_samples": len(train),
                },
            )
        )
    return anomalies
