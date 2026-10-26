"""链路诊断 Agent：定位耗时异常的服务与节点。默认关闭，可插拔。"""

from __future__ import annotations

from app.agents.base import BaseAgent, DiagnosisContext
from app.config.settings import get_settings
from app.models.diagnosis import DiagnosisFragment
from app.models.enums import AgentKind
from app.utils.logging import get_logger

logger = get_logger(__name__)

SLOW_THRESHOLD_MS = 800.0


class TraceAgent(BaseAgent):
    kind = AgentKind.TRACE
    label = "链路诊断"
    weight = 0.25

    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:
        if not get_settings().enable_trace_agent:
            return self.unavailable("ENABLE_TRACE_AGENT=false，链路诊断未启用")

        traces = ctx.traces_for() or ctx.trace_events
        if not traces:
            return self.unavailable("窗口内没有链路数据")

        slow = [t for t in traces if t.duration_ms >= SLOW_THRESHOLD_MS or t.error]
        slowest = max(traces, key=lambda t: t.duration_ms)
        evidence = [
            f"共 {len(traces)} 条链路，其中 {len(slow)} 条耗时超过 {SLOW_THRESHOLD_MS:.0f}ms",
            f"最慢：{slowest.service} {slowest.operation} {slowest.duration_ms:.0f}ms"
            f"（{slowest.span_count} spans）",
        ]
        if slow:
            services = sorted({t.service for t in slow})
            evidence.append("涉及服务：" + "、".join(services))

        ratio = slowest.duration_ms / SLOW_THRESHOLD_MS
        confidence = min(0.45 + ratio * 0.2, 0.85) if slow else 0.3
        conclusion = (
            f"慢调用集中在 {slowest.service}，最大耗时 {slowest.duration_ms:.0f}ms"
            if slow
            else "未发现显著慢调用，链路维度排除"
        )
        return DiagnosisFragment(
            agent=self.kind,
            confidence=round(confidence, 3),
            conclusion=conclusion,
            evidence=evidence,
        )
