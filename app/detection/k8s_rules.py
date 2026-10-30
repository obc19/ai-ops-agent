"""K8s 事件规则：把 Event / Pod 状态里的 reason 直接判成异常线索。

为什么单开一个检测器：K8s 的失败原因几乎全写在 ``reason`` 字段里，且大多是
**布尔型事实**（OOMKilled 就是发生过，不需要看曲线过没过阈值）。这类信号用
阈值规则表达不了，必须按 reason 白名单匹配。

这也是「没有 Prometheus / 没有 ELK 也能用」的关键路径：Pod 反复重启、被 OOM、
镜像拉不下来、调度不上，这些信息 K8s API 自己就有，Prometheus 和 ELK 反而是
二手货（ELK 甚至看不到 OOMKilled，因为容器已经死了，日志还没落盘）。
"""

from __future__ import annotations

import re
from typing import Any

from app.models.enums import Severity, SourceType
from app.models.events import AnomalyEvent, K8sEvent, TimeWindow
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 同一规则在窗口内累计达到这个次数才报，未配置则 1 次即报
DEFAULT_MIN_COUNT = 1


def _match(
    event: K8sEvent,
    reasons: set[str],
    pattern: re.Pattern[str] | None,
) -> bool:
    # K8s 的 reason 是 PascalCase 常量，比较统一忽略大小写
    if reasons and (event.reason or "").strip().lower() in reasons:
        return True
    # reason 对不上时再看正文：有些事件把真实原因埋在 message 里
    # （典型的 "Back-off restarting failed container" → CrashLoopBackOff）
    if pattern is not None:
        return bool(pattern.search(f"{event.reason} {event.message}"))
    return False


def detect_k8s_events(
    events: list[K8sEvent], rules: list[dict[str, Any]], window: TimeWindow
) -> list[AnomalyEvent]:
    """按 reason / 正则匹配 K8s 事件，同对象同规则合并计数。"""
    compiled: list[tuple[dict[str, Any], set[str], re.Pattern[str] | None]] = []
    for rule in rules:
        reasons = {str(r).strip().lower() for r in rule.get("reasons", []) if str(r).strip()}
        raw = rule.get("pattern")
        try:
            pattern = re.compile(str(raw), re.IGNORECASE) if raw else None
        except re.error as exc:
            logger.warning("K8s 规则 %s 的正则无效，已跳过：%s", rule.get("id"), exc)
            continue
        if not reasons and pattern is None:
            continue
        compiled.append((rule, reasons, pattern))

    if not compiled:
        return []

    # key = (namespace, target, rule_id) → 累计次数 / 最严重 / 最后一条消息
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        for rule, reasons, pattern in compiled:
            if not _match(event, reasons, pattern):
                continue
            rule_id = str(rule.get("id", "k8s-event"))
            key = (event.namespace, event.target, rule_id)
            slot = groups.setdefault(
                key,
                {
                    "rule": rule,
                    "cluster": event.cluster,
                    "count": 0,
                    "kind": event.kind,
                    "reason": event.reason,
                    "message": "",
                },
            )
            slot["count"] += int(event.count or 1)
            # 保留信息量最大的那条 message（最长），便于写进报告
            if len(event.message or "") > len(slot["message"]):
                slot["message"] = event.message
                slot["reason"] = event.reason or slot["reason"]
                slot["kind"] = event.kind or slot["kind"]
            break  # 一条事件只归属一条规则，避免重复放大

    anomalies: list[AnomalyEvent] = []
    for (namespace, target, rule_id), slot in groups.items():
        rule = slot["rule"]
        count = slot["count"]
        min_count = int(rule.get("min_count", DEFAULT_MIN_COUNT) or DEFAULT_MIN_COUNT)
        if count < min_count:
            continue
        anomalies.append(
            AnomalyEvent(
                source=SourceType.K8S,
                cluster=slot["cluster"],
                namespace=namespace,
                target=target,
                metric=rule_id,
                window=window,
                severity=Severity(rule.get("severity", "P1")),
                value=float(count),
                threshold=float(min_count),
                rule_id=rule_id,
                rule_name=str(rule.get("name", "")),
                summary=(
                    f"{namespace}/{target} {rule.get('name')}"
                    f"（{slot['reason'] or 'K8s 事件'} × {count}）"
                    + (f"：{slot['message'][:120]}" if slot["message"] else "")
                ),
                context={
                    "reason": slot["reason"],
                    "kind": slot["kind"],
                    "message": (slot["message"] or "")[:500],
                    "hint": rule.get("hint", ""),
                },
            )
        )
    return anomalies
