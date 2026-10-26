"""根因定位 Agent：汇总各维度片段 + RAG 经验，输出根因与处置建议。

实现策略（确认书 D12）：
1. 先由规则合成一份「保底结论」，保证任何情况下都有输出；
2. 若 LLM 可用，通过安全网关再求一次结构化结论；
3. LLM 失败/解析失败则沿用规则结论并标注，不影响主流程。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.agents.base import BaseAgent, DiagnosisContext
from app.config.settings import get_settings
from app.gateway.router import EgressBlocked, SafetyGateway, get_gateway
from app.knowledge.rag import RAGService
from app.llm.client import LLMError
from app.models.diagnosis import DiagnosisFragment
from app.models.enums import AgentKind
from app.utils.logging import get_logger

logger = get_logger(__name__)

ROOT_CAUSE_RULES: dict[str, tuple[str, list[str]]] = {
    "cpu_usage_percent": (
        "CPU 资源不足：容器 CPU 配额被打满，工作线程池耗尽，请求开始排队并出现限流",
        [
            "确认 order-service 副本数与 CPU limit 是否匹配当前流量",
            "排查是否存在死循环、同步阻塞调用或突发批量任务",
            "临时扩容副本 / 提高 CPU limit，观察线程池队列长度是否回落",
        ],
    ),
    "memory_usage_percent": (
        "内存不足或泄漏：堆内存耗尽触发 OOM，进程被内核或 K8s 驱逐",
        [
            "导出堆快照（jmap / pprof）分析对象增长趋势",
            "检查缓存、连接池、批量查询是否有未释放引用",
            "提高内存 limit 或调整 JVM/运行时内存参数作为止血手段",
        ],
    ),
    "http_latency_p99_ms": (
        "下游依赖变慢：调用链中下游服务响应超时，拖慢整体 P99 延迟",
        [
            "定位耗时最长的下游服务与接口，确认其自身健康度",
            "检查连接池大小、超时配置与重试策略是否放大延迟",
            "对慢接口增加熔断/限流，必要时降级非核心依赖",
        ],
    ),
    "http_error_rate_percent": (
        "上游服务不可用：依赖服务连接被拒或返回 5xx，错误率快速升高",
        [
            "确认依赖服务实例健康状态与最近发布记录",
            "检查网络策略、DNS 解析与端口连通性",
            "必要时回滚最近变更或切流到备用实例",
        ],
    ),
}


@dataclass
class RootCauseResult:
    fragment: DiagnosisFragment
    root_cause: str
    suggestion: list[str] = field(default_factory=list)
    used_llm: bool = False
    llm_error: str = ""


class RootCauseAgent(BaseAgent):
    kind = AgentKind.ROOT_CAUSE
    label = "根因定位"
    weight = 0.0

    def __init__(self, gateway: SafetyGateway | None = None) -> None:
        self.settings = get_settings()
        self.gateway = gateway or get_gateway()
        self.rag = RAGService()

    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:  # 兼容基类签名
        result = await self.synthesize(ctx, [])
        return result.fragment

    async def synthesize(
        self, ctx: DiagnosisContext, fragments: list[DiagnosisFragment]
    ) -> RootCauseResult:
        root_cause, suggestions, evidence = self._rule_based(ctx, fragments)
        confidence = self._weighted_confidence(fragments)

        if self.settings.llm_enabled:
            llm_result = await self._try_llm(ctx, fragments, root_cause, suggestions)
            if llm_result is not None:
                root_cause, suggestions, confidence = llm_result
                evidence.append("LLM 已参与根因归纳（经脱敏网关外发）")
                return RootCauseResult(
                    fragment=DiagnosisFragment(
                        agent=self.kind,
                        confidence=round(min(confidence, 0.95), 3),
                        conclusion=root_cause,
                        evidence=evidence,
                    ),
                    root_cause=root_cause,
                    suggestion=suggestions,
                    used_llm=True,
                )
            evidence.append("LLM 不可用或返回不合规，已使用规则结论兜底")

        return RootCauseResult(
            fragment=DiagnosisFragment(
                agent=self.kind,
                confidence=round(min(confidence, 0.9), 3),
                conclusion=root_cause,
                evidence=evidence,
                degraded=False,
                skip_reason=None if self.settings.llm_enabled else "未配置 LLM，使用规则引擎",
            ),
            root_cause=root_cause,
            suggestion=suggestions,
        )

    # ---------- 规则合成 ----------
    def _rule_based(
        self, ctx: DiagnosisContext, fragments: list[DiagnosisFragment]
    ) -> tuple[str, list[str], list[str]]:
        root_cause, suggestions = ROOT_CAUSE_RULES.get(
            ctx.metric,
            (
                f"{ctx.target} 出现 {ctx.metric} 异常，需结合日志与链路进一步确认",
                ["核查该对象的资源配额与最近变更记录", "对比同期基线，确认是否为业务量突增"],
            ),
        )
        evidence: list[str] = [f"异常类型命中规则：{ctx.anomaly.rule_name or ctx.metric}"]

        log_hints = [
            f.conclusion for f in fragments if f.agent is AgentKind.LOG and not f.degraded
        ]
        if log_hints:
            evidence.append(f"日志维度佐证：{log_hints[0]}")

        if ctx.rag_hits:
            top = ctx.rag_hits[0]
            evidence.append(
                f"历史经验命中「{top.title}」（相似度 {top.score:.2f}）：{top.root_cause}"
            )
            if top.score >= 0.5 and top.resolution:
                suggestions = list(dict.fromkeys(top.resolution + suggestions))[:5]
                root_cause = f"{root_cause}（与历史案例「{top.title}」一致）"

        return root_cause, suggestions, evidence

    @staticmethod
    def _weighted_confidence(fragments: list[DiagnosisFragment]) -> float:
        weights = {
            AgentKind.METRIC: 0.45,
            AgentKind.LOG: 0.35,
            AgentKind.TRACE: 0.2,
        }
        total = 0.0
        valid = 0.0
        for fragment in fragments:
            if fragment.agent is AgentKind.ROOT_CAUSE or fragment.degraded:
                continue
            if fragment.confidence <= 0:
                continue
            weight = weights.get(fragment.agent, 0.1)
            total += fragment.confidence * weight
            valid += weight
        if valid == 0:
            return 0.35
        return min(total / valid * 0.95, 0.9)

    # ---------- LLM 合成 ----------
    async def _try_llm(
        self,
        ctx: DiagnosisContext,
        fragments: list[DiagnosisFragment],
        fallback_cause: str,
        fallback_steps: list[str],
    ) -> tuple[str, list[str], float] | None:
        prompt = self._build_prompt(ctx, fragments)
        try:
            egress = await self.gateway.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是资深 SRE，请基于给定证据判定故障根因，给出可执行处置步骤。"
                            "只输出 JSON：{\"root_cause\": string, \"suggestion\": string[], "
                            "\"confidence\": number}，不要输出多余文字。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                purpose="root_cause",
                temperature=0.1,
                max_tokens=600,
            )
        except (LLMError, EgressBlocked) as exc:
            logger.warning("根因 LLM 调用失败，使用规则结论：%s", exc)
            return None

        payload = self._extract_json(egress.text)
        if not payload:
            logger.warning("LLM 返回无法解析，使用规则结论")
            return None
        cause = str(payload.get("root_cause") or fallback_cause)
        steps = [str(s) for s in payload.get("suggestion") or fallback_steps][:5]
        try:
            confidence = float(payload.get("confidence", 0.75))
        except (TypeError, ValueError):
            confidence = 0.75
        return cause, steps or fallback_steps, max(min(confidence, 0.95), 0.3)

    @staticmethod
    def _build_prompt(ctx: DiagnosisContext, fragments: list[DiagnosisFragment]) -> str:
        lines = [
            f"异常对象：{ctx.anomaly.cluster}/{ctx.anomaly.namespace}/{ctx.anomaly.target}",
            f"异常指标：{ctx.anomaly.metric}，当前 {ctx.anomaly.value}，阈值 {ctx.anomaly.threshold}",
            f"严重级别：{ctx.anomaly.severity.value}",
            f"时间窗口：{ctx.window.label()}",
            "各维度分析片段：",
        ]
        lines.extend(f"- {f.brief()}" for f in fragments if f.agent is not AgentKind.ROOT_CAUSE)
        rag_block = RAGService.build_prompt_block(ctx.rag_hits)
        if rag_block:
            lines.append(rag_block)
        return "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
