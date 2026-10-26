"""Mock 世界：没有真实 Prometheus / ELK / Jaeger / K8s 时提供可注入故障的仿真数据。

设计要点：
- 数据确定性（固定种子），便于测试断言与验收复现；
- 故障可注入（inject_fault），用于演示「异常 → 检测 → 诊断 → 告警」闭环；
- 对外只暴露与真实数据源同构的查询结果，适配器无需区分。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models.events import TimeWindow
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

METRIC_CATALOG: dict[str, dict[str, float]] = {
    "cpu_usage_percent": {"baseline": 32.0, "spike": 94.0, "unit": "%"},
    "memory_usage_percent": {"baseline": 54.0, "spike": 91.0, "unit": "%"},
    "http_latency_p99_ms": {"baseline": 180.0, "spike": 2400.0, "unit": "ms"},
    "http_error_rate_percent": {"baseline": 0.4, "spike": 18.0, "unit": "%"},
}


@dataclass
class Fault:
    target: str
    metric: str = "cpu_usage_percent"
    severity: str = "P1"
    magnitude: float = 1.0
    namespace: str = "prod"
    cluster: str = "prod-cluster"
    note: str = ""
    injected_at: datetime = field(default_factory=utc_now)

    def key(self) -> str:
        return f"{self.cluster}/{self.namespace}/{self.target}/{self.metric}"


class MockWorld:
    """单例仿真环境。"""

    def __init__(self) -> None:
        self.cluster = "prod-cluster"
        self.namespace = "prod"
        self.services = ["order-service", "payment-service", "user-service"]
        self.targets = [f"{s}" for s in self.services] + ["node-1", "node-2"]
        self._faults: dict[str, Fault] = {}
        self._rng = random.Random(20261026)

    # ---------- 故障注入 ----------
    def inject_fault(
        self,
        target: str,
        metric: str = "cpu_usage_percent",
        severity: str = "P1",
        magnitude: float = 1.0,
        note: str = "",
    ) -> Fault:
        fault = Fault(
            target=target, metric=metric, severity=severity, magnitude=magnitude, note=note
        )
        self._faults[fault.key()] = fault
        logger.warning("已注入 mock 故障：%s / %s（%s）", target, metric, severity)
        return fault

    def clear_faults(self) -> int:
        count = len(self._faults)
        self._faults.clear()
        if count:
            logger.info("已清除 %d 个 mock 故障", count)
        return count

    def list_faults(self) -> list[Fault]:
        return list(self._faults.values())

    def fault_for(self, target: str, metric: str) -> Fault | None:
        return self._faults.get(f"{self.cluster}/{self.namespace}/{target}/{metric}")

    def _faulted_targets(self, metric: str) -> list[str]:
        return [f.target for f in self._faults.values() if f.metric == metric]

    # ---------- 指标 ----------
    def metric_series(
        self, target: str, metric: str, window: TimeWindow, points: int = 24
    ) -> list[tuple[datetime, float]]:
        spec = METRIC_CATALOG.get(metric, {"baseline": 20.0, "spike": 90.0, "unit": ""})
        fault = self.fault_for(target, metric)
        total = max(int(window.duration_seconds), 60)
        step = total / max(points - 1, 1)

        rng = random.Random(f"{target}:{metric}:{int(window.start.timestamp())}")
        series: list[tuple[datetime, float]] = []
        for idx in range(points):
            ts = window.start + timedelta(seconds=step * idx)
            base = spec["baseline"] * (1 + math.sin(idx / 3.0) * 0.08)
            noise = rng.uniform(-2.0, 2.0)
            value = base + noise
            if fault:
                # 后 60% 的时间点进入尖峰，模拟故障逐步显性化
                ratio = idx / max(points - 1, 1)
                if ratio >= 0.4:
                    ramp = min((ratio - 0.4) / 0.4, 1.0) * fault.magnitude
                    value = value * (1 - ramp) + spec["spike"] * ramp
            series.append((ts, round(max(value, 0.0), 2)))
        return series

    def latest_metric(self, target: str, metric: str) -> float:
        window = TimeWindow(start=utc_now() - timedelta(minutes=5), end=utc_now())
        return self.metric_series(target, metric, window, points=2)[-1][1]

    # ---------- 日志 ----------
    def logs(self, window: TimeWindow, target: str | None = None) -> list[dict]:
        rng = random.Random(f"log:{int(window.start.timestamp())}:{target}")
        records: list[dict] = []
        for fault in self._faults.values():
            if target and fault.target != target:
                continue
            templates = {
                "cpu_usage_percent": [
                    (f"{fault.target}", "ERROR", "worker pool exhausted: cpu throttling detected"),
                    (f"{fault.target}", "WARN", "request queue length 1280 exceeds limit 512"),
                ],
                "memory_usage_percent": [
                    (fault.target, "ERROR", "java.lang.OutOfMemoryError: Java heap space"),
                    (fault.target, "WARN", "GC overhead limit exceeded, pause 1.8s"),
                ],
                "http_latency_p99_ms": [
                    (fault.target, "WARN", "upstream timeout after 3000ms calling payment-gateway"),
                ],
                "http_error_rate_percent": [
                    (fault.target, "ERROR", "HTTP 503 from upstream: connection refused"),
                ],
            }.get(fault.metric, [(fault.target, "ERROR", "unknown fault")])
            for idx, (svc, level, message) in enumerate(templates):
                for repeat in range(3):
                    ts = window.end - timedelta(seconds=rng.randint(10, 300))
                    records.append(
                        {
                            "timestamp": ts.isoformat(),
                            "service": svc,
                            "level": level,
                            "message": message,
                            "count": 6 + idx * 4 + repeat,
                        }
                    )
        if not self._faults:  # 无故障时也返回少量背景日志
            ts = window.end - timedelta(seconds=120)
            records.append(
                {
                    "timestamp": ts.isoformat(),
                    "service": target or self.services[0],
                    "level": "INFO",
                    "message": "healthcheck ok",
                    "count": 1,
                }
            )
        return records

    # ---------- 链路 ----------
    def traces(self, window: TimeWindow) -> list[dict]:
        faulted = self._faulted_targets("http_latency_p99_ms") + self._faulted_targets(
            "cpu_usage_percent"
        )
        rows = []
        for svc in self.services:
            slow = svc in faulted
            rows.append(
                {
                    "service": svc,
                    "operation": f"GET /{svc}/api/v1/query",
                    "trace_id": f"trace-{svc}-{int(window.end.timestamp())}",
                    "duration_ms": 2380.0 if slow else 165.0,
                    "span_count": 12 if slow else 5,
                    "error": slow,
                }
            )
        return rows

    # ---------- K8s 事件 ----------
    def k8s_events(self, window: TimeWindow) -> list[dict]:
        rows = []
        for fault in self._faults.values():
            rows.append(
                {
                    "reason": "Unhealthy" if "cpu" in fault.metric else "OOMKilled",
                    "kind": "Pod",
                    "target": fault.target,
                    "message": f"pod {fault.target}-7d9f8c 重启：{fault.metric} 超限",
                    "count": 4,
                    "timestamp": window.end.isoformat(),
                }
            )
        return rows


world = MockWorld()
