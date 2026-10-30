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
    # ================= 应用 / 容器层 =================
    "cpu_usage_percent": (
        "{target} 的 CPU 资源不足：配额被打满，工作线程排队并开始限流",
        [
            "确认 {target} 的副本数与 CPU limit 是否匹配当前流量",
            "排查是否存在死循环、同步阻塞调用或突发批量任务",
            "临时扩容副本 / 提高 CPU limit，观察线程池队列是否回落",
        ],
    ),
    "memory_usage_percent": (
        "{target} 内存不足或泄漏：堆内存耗尽触发 OOM，进程被内核或 K8s 驱逐",
        [
            "导出堆快照（jmap / pprof）分析对象增长趋势",
            "检查缓存、连接池、批量查询是否有未释放引用",
            "提高内存 limit 或调整运行时内存参数作为止血手段",
        ],
    ),
    "http_latency_p99_ms": (
        "{target} 下游依赖变慢：调用链中下游服务响应超时，拖慢整体 P99",
        [
            "定位耗时最长的下游服务与接口，确认其自身健康度",
            "检查连接池大小、超时配置与重试策略是否放大延迟",
            "对慢接口增加熔断/限流，必要时降级非核心依赖",
        ],
    ),
    "http_error_rate_percent": (
        "{target} 上游服务不可用：依赖服务连接被拒或返回 5xx，错误率快速升高",
        [
            "确认依赖服务实例健康状态与最近发布记录",
            "检查网络策略、DNS 解析与端口连通性",
            "必要时回滚最近变更或切流到备用实例",
        ],
    ),
    # ================= 服务器本体层 =================
    "host_load_per_core": (
        "服务器 CPU 已被跑满，进程在排队：每核负载超过 2 意味着请求需要等待调度",
        [
            "top -o %CPU 找出占用最高的进程，再 pidstat -u 1 5 确认是否持续",
            "若为业务进程：检查死循环、同步阻塞、突发批任务，必要时限流或扩容",
            "若 %sy 偏高：查系统调用与中断；若 kswapd 偏高：转查内存不足",
            "临时缓解可迁移部分流量到其他实例，或调整进程优先级",
        ],
    ),
    "host_cpu_usage_percent": (
        "服务器 CPU 使用率接近饱和，处理能力已到上限",
        [
            "top -o %CPU 定位消耗最高的进程与线程（top -Hp <pid>）",
            "pidstat -u 1 5 观察是持续高还是周期性尖峰",
            "确认是否与业务量增长或某个批任务的时间点吻合",
            "持续高位则扩容实例或升级 CPU 规格；尖峰则错峰调度",
        ],
    ),
    "host_cpu_iowait_percent": (
        "磁盘 IO 成为瓶颈：CPU 在等待 IO 完成，请求响应被拖慢",
        [
            "iostat -x 1 5 看哪块盘 %util 接近 100、await 是否显著升高",
            "iotop -o 找出读写最猛的进程",
            "排查日志暴涨、大文件写入、数据库全表扫描或缺少索引",
            "临时停掉非关键批任务、清理日志；根本方案是换 SSD 或拆分 IO",
        ],
    ),
    "host_memory_used_percent": (
        "服务器内存接近耗尽，可能触发 OOM Killer 杀掉进程",
        [
            "free -h 看可用内存与缓存，ps aux --sort=-%mem | head 找内存大户",
            "区分「持续增长（泄漏）」与「一次性峰值（流量/批任务）」",
            "泄漏：重启进程止血并抓取堆快照分析；峰值：扩容或调小缓存",
            "检查 dmesg | grep -i oom 是否有进程已被杀",
        ],
    ),
    "host_swap_used_percent": (
        "已开始使用 Swap：物理内存不足，换页会显著拖慢所有进程",
        [
            "free -h 确认 Swap 使用量与被换出的进程",
            "cat /proc/sys/vm/swappiness 检查换页倾向，服务型机器建议调低",
            "找出内存占用最高且不常访问的进程，考虑迁移或限流",
            "根本解决是增加物理内存，Swap 只能作为应急缓冲",
        ],
    ),
    "host_disk_used_percent": (
        "磁盘空间不足，继续写入可能失败并导致服务不可用",
        [
            "df -h 定位是哪个分区写满，du -sh /* | sort -h 找大目录",
            "检查日志是否未切割：journalctl --vacuum-size=500M，清理旧日志与压缩包",
            "清理 core dump、临时文件、无用镜像（docker system prune）",
            "长期措施：扩容磁盘、配置日志轮转与容量告警阈值",
        ],
    ),
    "host_fd_used_percent": (
        "文件描述符接近系统上限，新连接会因无法分配 fd 而失败",
        [
            "cat /proc/sys/fs/file-nr 确认已用与上限",
            "lsof -p <pid> | wc -l 找出 fd 占用最多的进程",
            "确认是连接未释放（连接池/泄漏）还是并发量本身过高",
            "应急：sysctl -w fs.file-max=<更大值>，进程级 ulimit -n 调高；根本是修泄漏",
        ],
    ),
    "host_zombie_processes": (
        "存在僵尸进程：子进程已退出但父进程未回收，持续累积会耗尽进程表",
        [
            "ps -eo pid,ppid,stat,cmd | awk '$3 ~ /Z/ {print}' 找出僵尸及其父进程",
            "确认父进程是否还在正常运行（父进程不调用 wait 就会持续产生僵尸）",
            "僵尸进程本身 kill -9 无效，只能由父进程回收或重启父进程",
            "若父进程是业务代码，检查 fork 后是否遗漏 waitpid",
        ],
    ),
    "host_tcp_established": (
        "服务器连接数异常增长，可能是流量突增、重试风暴或连接泄漏",
        [
            "ss -s 看连接总数与各状态分布",
            "ss -tan | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head 找来源",
            "区分正常流量增长、上游重试放大、还是连接未复用导致泄漏",
            "检查上游重试策略与连接池配置，异常来源可临时限流",
        ],
    ),
    # ================= 主动拨测层（用户视角）=================
    "probe_total_ms": (
        "「{target}」端到端访问变慢，需按分段耗时定位是网络、DNS 还是服务端",
        [
            "对比同一目标的 probe_dns_ms / probe_connect_ms / probe_tls_ms / probe_ttfb_ms",
            "TTFB 占大头 → 服务端处理慢，转查该服务 CPU、内存与下游依赖",
            "DNS 或建连占大头 → 网络链路问题，用 mtr 与 dig 进一步确认",
            "多个目标同时变慢通常指向本地出口网络；仅单个变慢则是目标自身问题",
        ],
    ),
    "probe_ttfb_ms": (
        "「{target}」服务端首字节返回慢：请求已到达，但服务处理耗时长",
        [
            "确认目标服务自身的 CPU、内存、GC 与线程池状态",
            "检查下游依赖耗时：数据库慢查询、缓存未命中、第三方接口超时",
            "查看应用日志中的慢请求与锁等待",
            "临时扩容实例或加缓存；根本是优化慢接口与查询",
        ],
    ),
    "probe_dns_ms": (
        "「{target}」DNS 解析慢：请求还没发出就卡在域名解析上",
        [
            "dig <域名> +stats 看解析耗时，分别测默认 DNS 与公共 DNS",
            "检查 /etc/resolv.conf：是否配置了不可达或跨地域的 DNS 服务器",
            "内网域名需确认 CoreDNS / 内建 DNS 的负载与上游转发",
            "启用本地 DNS 缓存（systemd-resolved / nscd）可显著降低平均耗时",
        ],
    ),
    "probe_connect_ms": (
        "「{target}」TCP 建连慢：网络链路或目标端 accept 队列存在问题",
        [
            "ping 与 mtr 确认丢包率和逐跳延迟",
            "检查防火墙、安全组是否限速，netstat -s | grep -i listen 看 SYN 队列溢出",
            "确认带宽是否打满（iftop / sar -n DEV 1）",
            "跨地域访问考虑就近接入或 CDN，目标端检查 backlog 配置",
        ],
    ),
    "probe_ok": (
        "「{target}」拨测失败：目标不可访问或返回了非预期状态码",
        [
            "先手动 curl -v <url> 复现，判断是网络不通还是服务报错",
            "服务端：确认进程存活、端口监听（ss -lntp）、网关后端健康状态",
            "https 目标：检查证书是否过期（openssl s_client -connect host:443）",
            "确认最近是否有发布或配置变更，必要时回滚",
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
        template, steps = ROOT_CAUSE_RULES.get(
            ctx.metric,
            (
                f"{{target}} 出现 {ctx.metric} 异常，需结合日志与链路进一步确认",
                ["核查该对象的资源配额与最近变更记录", "对比同期基线，确认是否为业务量突增"],
            ),
        )
        # 结论与步骤里的 {target} 都替换成真实对象，避免出现别处的服务名
        root_cause = template.replace("{target}", ctx.target or ctx.anomaly.target)
        suggestions = [
            step.replace("{target}", ctx.target or ctx.anomaly.target) for step in steps
        ]
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
