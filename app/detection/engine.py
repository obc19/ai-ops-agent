"""检测引擎：串起阈值 / 环比 / 基线 / 日志规则，统一去重与排序。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.detection.baseline import detect_baseline
from app.detection.k8s_rules import detect_k8s_events
from app.detection.log_rules import detect_logs
from app.detection.threshold import detect_percent_change, detect_static
from app.models.events import (
    AnomalyEvent,
    BaseEvent,
    K8sEvent,
    LogEvent,
    MetricEvent,
    TimeWindow,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_ANOMALIES = 30


def _default_rules_path() -> Path:
    """规则文件来自配置目录（可挂载），不再写死在代码包里。"""
    from app.config.settings import get_settings

    return get_settings().detection_rules_path


class DetectionEngine:
    """无状态检测引擎，规则来自 YAML，可在运行时热重载。"""

    def __init__(self, rules_path: Path | None = None) -> None:
        self.rules_path = rules_path or _default_rules_path()
        self._rules: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self._rules = yaml.safe_load(self.rules_path.read_text(encoding="utf-8")) or {}
            logger.info(
                "检测规则已加载：阈值 %d 条 / 环比 %d 条 / 日志 %d 条 / K8s %d 条",
                len(self._rules.get("thresholds", [])),
                len(self._rules.get("percent_change", [])),
                len(self._rules.get("log_rules", [])),
                len(self._rules.get("k8s_events", [])),
            )
        except (OSError, yaml.YAMLError) as exc:
            # fail-closed：规则加载失败时宁可不出结论，也不误判
            logger.error("检测规则加载失败，检测将被跳过：%s", exc)
            self._rules = {}

    @property
    def loaded(self) -> bool:
        return bool(self._rules)

    def detect(self, events: list[BaseEvent], window: TimeWindow) -> list[AnomalyEvent]:
        if not self.loaded:
            logger.warning("规则未加载，跳过本轮检测")
            return []

        metrics = [e for e in events if isinstance(e, MetricEvent)]
        logs = [e for e in events if isinstance(e, LogEvent)]
        k8s_events = [e for e in events if isinstance(e, K8sEvent)]

        anomalies: list[AnomalyEvent] = []
        anomalies += detect_static(metrics, self._rules.get("thresholds", []), window)
        anomalies += detect_percent_change(
            metrics, self._rules.get("percent_change", []), window
        )
        anomalies += detect_baseline(metrics, self._rules.get("baseline", {}), window)
        anomalies += detect_logs(logs, self._rules.get("log_rules", []), window)
        # K8s 事件是「布尔型事实」，不走阈值；没有 k8s_events 段就静默跳过
        anomalies += detect_k8s_events(k8s_events, self._rules.get("k8s_events", []), window)

        result = self._dedupe(anomalies) if self._rules.get("dedupe", True) else anomalies
        result.sort(key=lambda a: (a.severity.rank, a.source.value, a.target))
        if len(result) > MAX_ANOMALIES:
            logger.warning("异常数量 %d 超过上限，仅保留优先级最高的 %d 条", len(result), MAX_ANOMALIES)
            result = result[:MAX_ANOMALIES]
        return result

    @staticmethod
    def _dedupe(anomalies: list[AnomalyEvent]) -> list[AnomalyEvent]:
        """同一对象 + 同一指标只保留级别最高的一条（同级别保留先到的）。"""
        best: dict[tuple[str, str, str, str, str], AnomalyEvent] = {}
        for item in anomalies:
            key = (item.source.value, item.cluster, item.namespace, item.target, item.metric)
            current = best.get(key)
            if current is None or item.severity.rank < current.severity.rank:
                best[key] = item
        return list(best.values())


_engine: DetectionEngine | None = None


def get_engine() -> DetectionEngine:
    global _engine
    if _engine is None:
        _engine = DetectionEngine()
    return _engine
