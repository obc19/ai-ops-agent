"""日志规则检测：关键词 / 正则命中即视为异常线索。"""

from __future__ import annotations

import re
from typing import Any

from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, LogEvent, TimeWindow


def detect_logs(
    events: list[LogEvent], rules: list[dict[str, Any]], window: TimeWindow
) -> list[AnomalyEvent]:
    compiled = [
        (rule, re.compile(str(rule.get("pattern", "")), re.IGNORECASE))
        for rule in rules
        if rule.get("pattern")
    ]
    anomalies: list[AnomalyEvent] = []
    for event in events:
        if not event.message:
            continue
        for rule, pattern in compiled:
            if not pattern.search(event.message):
                continue
            anomalies.append(
                AnomalyEvent(
                    source=SourceType.LOG,
                    cluster=event.cluster,
                    namespace=event.namespace,
                    target=event.target,
                    metric=str(rule.get("id", "log")),
                    window=window,
                    severity=Severity(rule.get("severity", "P1")),
                    value=float(event.count),
                    threshold=None,
                    rule_id=str(rule.get("id", "")),
                    rule_name=str(rule.get("name", "")),
                    summary=(
                        f"{event.target} 日志命中「{rule.get('name')}」"
                        f"（{event.count} 条）：{event.message[:120]}"
                    ),
                    context={
                        "level": event.level,
                        "index": event.index,
                        "message": event.message[:500],
                    },
                )
            )
            break  # 同一日志只归属一条规则，避免重复放大
    return anomalies
