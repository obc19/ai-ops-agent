"""指标诊断 Agent：趋势、峰值、资源瓶颈判定。"""

from __future__ import annotations

from app.agents.base import BaseAgent, DiagnosisContext
from app.models.diagnosis import DiagnosisFragment
from app.models.enums import AgentKind
from app.utils.logging import get_logger

logger = get_logger(__name__)

BOTTLENECK_HINTS = {
    "cpu_usage_percent": "CPU 资源瓶颈",
    "memory_usage_percent": "内存资源瓶颈",
    "http_latency_p99_ms": "响应延迟异常",
    "http_error_rate_percent": "错误率异常",
}


class MetricAgent(BaseAgent):
    kind = AgentKind.METRIC
    label = "指标诊断"
    weight = 0.4

    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:
        events = ctx.metrics_for()
        if not events:
            return self.skip(f"窗口内没有 {ctx.metric} 的指标序列")

        event = events[0]
        samples = event.samples
        if not samples:
            return self.skip("指标序列为空")

        values = [s.value for s in samples]
        first, last, peak = values[0], values[-1], max(values)
        change = (last - first) / first * 100 if first else 0.0
        unit = event.unit or ""

        evidence = [
            f"窗口内共 {len(samples)} 个采样点",
            f"起始 {first:.1f}{unit} → 当前 {last:.1f}{unit}，变化 {change:+.1f}%",
            f"峰值 {peak:.1f}{unit}（阈值 {ctx.anomaly.threshold}{unit}）",
        ]
        if len(values) > 2:
            tail = values[len(values) // 2 :]
            evidence.append(f"后半窗口均值 {sum(tail) / len(tail):.1f}{unit}")

        hint = BOTTLENECK_HINTS.get(ctx.metric, "指标异常")
        sustained = sum(1 for v in values if ctx.anomaly.threshold and v >= ctx.anomaly.threshold)
        confidence = 0.5 + min(sustained / max(len(values), 1), 1.0) * 0.3
        if abs(change) > 40:
            confidence += 0.08

        conclusion = (
            f"{ctx.target} 的 {ctx.metric} 持续走高（峰值 {peak:.1f}{unit}），"
            f"判定为{hint}"
        )
        return DiagnosisFragment(
            agent=self.kind,
            confidence=round(min(confidence, 0.92), 3),
            conclusion=conclusion,
            evidence=evidence,
        )
