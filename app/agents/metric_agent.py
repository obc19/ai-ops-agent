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
    "host_load_per_core": "CPU 排队，处理能力饱和",
    "host_cpu_usage_percent": "CPU 使用率饱和",
    "host_cpu_iowait_percent": "磁盘 IO 瓶颈",
    "host_memory_used_percent": "内存即将耗尽",
    "host_swap_used_percent": "内存不足已开始换页",
    "host_disk_used_percent": "磁盘空间不足",
    "host_fd_used_percent": "文件描述符接近耗尽",
    "host_zombie_processes": "僵尸进程堆积",
    "host_tcp_established": "连接数异常增长",
    "probe_total_ms": "端到端访问变慢",
    "probe_ttfb_ms": "服务端处理慢",
    "probe_dns_ms": "DNS 解析慢",
    "probe_connect_ms": "网络建连慢",
    "probe_ok": "目标不可用",
}


class MetricAgent(BaseAgent):
    kind = AgentKind.METRIC
    label = "指标诊断"
    weight = 0.4

    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:
        events = ctx.metrics_for()
        fallback = False
        if not events:
            # 触发异常的是日志/事件规则（metric 形如 log-timeout），不是指标名。
            # 此时不直接降级——退而看该对象在窗口内偏离最大的指标，
            # 否则日志一触发，指标维度就永远是 0，报告置信度被无谓拉低。
            candidates = [e for e in ctx.metric_events if e.target == ctx.target and e.samples]
            if not candidates:
                return self.skip(f"窗口内既没有 {ctx.metric} 序列，也没有 {ctx.target} 的任何指标")
            best = max(candidates, key=self._deviation)
            if self._deviation(best) < 10:
                return self.skip(
                    f"{ctx.target} 的指标均无显著偏离（最大仅 {self._deviation(best):.1f}%），"
                    "异常未在指标层体现"
                )
            events = [best]
            fallback = True

        event = events[0]
        samples = event.samples
        if not samples:
            return self.skip("指标序列为空")

        values = [s.value for s in samples]
        first, last, peak = values[0], values[-1], max(values)
        change = (last - first) / first * 100 if first else 0.0
        unit = event.unit or ""
        metric = event.metric
        threshold = None if fallback else ctx.anomaly.threshold

        evidence = []
        if fallback:
            evidence.append(
                f"触发来源为日志/事件规则（{ctx.metric}），改看该对象偏离最大的指标 {metric}"
            )
        evidence.extend(
            [
                f"窗口内共 {len(samples)} 个采样点",
                f"起始 {first:.1f}{unit} → 当前 {last:.1f}{unit}，变化 {change:+.1f}%",
                f"峰值 {peak:.1f}{unit}" + (f"（阈值 {threshold}{unit}）" if threshold else ""),
            ]
        )
        if len(values) > 2:
            tail = values[len(values) // 2 :]
            evidence.append(f"后半窗口均值 {sum(tail) / len(tail):.1f}{unit}")

        hint = BOTTLENECK_HINTS.get(metric, "指标异常")
        if threshold:
            sustained = sum(1 for v in values if v >= threshold)
        else:
            # 没有阈值时以「后半窗口相对前半窗口抬升 50%」作为持续异常判据
            half = max(len(values) // 2, 1)
            base = (sum(values[:half]) / half) or 1e-9
            sustained = sum(1 for v in values[half:] if v > base * 1.5)
        confidence = 0.5 + min(sustained / max(len(values), 1), 1.0) * 0.3
        if abs(change) > 40:
            confidence += 0.08
        if fallback:
            confidence *= 0.9  # 非直接触发指标，置信度略降

        conclusion = (
            f"{ctx.target} 的 {metric} 持续走高（峰值 {peak:.1f}{unit}），"
            f"判定为{hint}"
        )
        return DiagnosisFragment(
            agent=self.kind,
            confidence=round(min(confidence, 0.92), 3),
            conclusion=conclusion,
            evidence=evidence,
        )

    @staticmethod
    def _deviation(event) -> float:
        """首尾相对变化幅度，用于在没有明确指标时挑最异常的那个。"""
        values = [s.value for s in event.samples]
        if len(values) < 2:
            return 0.0
        first, last = values[0], values[-1]
        if not first:
            return abs(last)
        return abs((last - first) / first * 100)
