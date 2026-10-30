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
    # ================= 日志规则触发（此时 metric 是规则 id，不是指标名）=================
    "log-timeout": (
        "{target} 上游调用超时：下游依赖响应超过阈值，请求在等待中耗尽线程或连接",
        [
            "从日志里取出超时的下游服务名与接口，确认它自身是否健康",
            "检查调用方超时与重试配置：重试次数过多会把下游的慢放大成雪崩",
            "确认连接池是否被打满（等待获取连接的时间同样计入超时）",
            "临时提高超时或降级非核心依赖；根本是修复下游变慢的原因",
        ],
    ),
    "log-conn": (
        "{target} 连接被拒：目标端口未监听、服务未启动，或被网络策略拦截",
        [
            "ss -lntp 确认目标端口是否在监听、进程是否存在",
            "确认最近是否有发布、重启或扩缩容导致实例暂时下线",
            "检查网络策略 / 安全组 / iptables 是否放通该端口",
            "若目标是数据库等中间件：确认连接数与最大连接限制是否已满",
        ],
    ),
    "log-oom": (
        "{target} 发生内存溢出：堆或容器内存耗尽，进程可能已被 OOM Killer 终止",
        [
            "dmesg -T | grep -i 'killed process' 确认是否被内核杀掉",
            "导出堆快照（jmap -dump / pprof）分析对象增长趋势",
            "检查是否有一次性大批量加载、缓存无上限、集合未清理",
            "临时提高内存上限止血；根本是修掉泄漏点",
        ],
    ),
    "log-disk": (
        "{target} 磁盘写满：无法继续写入日志或数据，服务可能进入只读或直接崩溃",
        [
            "df -h 定位分区，du -sh /* | sort -h 找出大目录",
            "清理日志：journalctl --vacuum-size=500M，删除旧压缩包与 core dump",
            "确认日志轮转（logrotate）是否生效，是否被应用高速写入撑爆",
            "长期：扩容磁盘 + 容量告警阈值 + 应用侧降低日志级别",
        ],
    ),
    "log-5xx": (
        "{target} 持续返回 5xx：服务端处理失败，通常是内部异常或下游不可用",
        [
            "从日志取 5xx 对应的异常栈，定位抛错的具体代码路径",
            "确认是否集中在某个接口、某类参数或某个下游依赖",
            "检查最近发布记录，必要时回滚",
            "对已知失败路径增加降级与兜底，避免整体不可用",
        ],
    ),
    "log-cpu-throttle": (
        "{target} 出现 CPU 限流与队列积压：处理能力不足，请求开始排队",
        [
            "确认容器 CPU limit 与实际用量，是否长期贴着 limit",
            "top -Hp <pid> 看是哪个线程在消耗，区分业务线程与 GC/后台线程",
            "检查是否有死循环、同步阻塞调用或不合理的批处理",
            "临时扩容副本或提高 CPU limit；根本是优化 CPU 热点",
        ],
    ),
    # ================= K8s 原生信号（无 Prometheus / 无 ELK 也拿得到）=================
    # 事件类（metric = 规则 id）
    "k8s-crashloop": (
        "{target} 容器反复崩溃：进程启动后很快退出，K8s 进入退避重启",
        [
            "kubectl logs <pod> --previous 看上一次退出前的日志（关键：不是当前实例）",
            "kubectl describe pod <pod> 看 Last State 的 Exit Code：1=应用报错，137=被杀/OOM，143=正常终止信号",
            "确认启动依赖是否就绪：数据库/配置中心/下游服务，以及启动探针时限是否过短",
            "临时把副本数拉起保证可用，再用稳定镜像回滚止血",
        ],
    ),
    "k8s-oomkilled": (
        "{target} 容器被 OOMKilled：内存用量超过容器 limit，被内核直接杀死",
        [
            "kubectl top pod <pod> 看实际用量，对比 resources.limits.memory 是否明显偏小",
            "确认是稳态超限还是启动/批处理瞬时峰值（后者可调大 limit 或降低并发）",
            "JVM 需确认 -Xmx 小于 limit（建议 70%~80%），否则堆外+堆内会一起撞顶",
            "怀疑泄漏则导堆分析；单纯加 limit 只治标",
        ],
    ),
    "k8s-image-pull": (
        "{target} 镜像拉取失败：Pod 卡在 ErrImagePull / ImagePullBackOff，容器从未启动",
        [
            "核对镜像名与 tag 是否存在（docker manifest inspect / 仓库页面）",
            "私有仓库确认 imagePullSecrets 已配置且未过期（kubectl get secret）",
            "节点侧确认能访问仓库：kubectl describe pod 里的事件会写明 DNS/超时/401",
            "集群拉外网镜像需代理或镜像同步；确认节点容器运行时配置",
        ],
    ),
    "k8s-config-error": (
        "{target} 容器配置错误：引用的 ConfigMap / Secret 缺失或键名不匹配，容器创建失败",
        [
            "kubectl describe pod <pod> 事件里通常会直接写出缺失的对象名",
            "确认 ConfigMap/Secret 与 Pod 在同一命名空间",
            "核对 envFrom / volume 引用的 key 是否真实存在（keys 拼写、大小写）",
            "Secret 用 stringData 还是 data（后者需 base64），改完重启 Pod 生效",
        ],
    ),
    "k8s-unschedulable": (
        "{target} 无法调度：集群里没有满足条件的节点，Pod 一直 Pending",
        [
            "kubectl describe pod 看调度器给出的具体原因（Insufficient cpu/memory、node affinity 等）",
            "kubectl describe node 看 Allocated resources 是否已接近满载",
            "检查 nodeSelector / affinity / tolerations 是否与节点标签、污点匹配",
            "若 PVC 未绑定也会导致 Pending：kubectl get pvc 确认 Bound 状态",
        ],
    ),
    "k8s-mount-failed": (
        "{target} 存储挂载失败：卷无法挂到节点或容器上",
        [
            "kubectl get pvc / pv 确认绑定状态与 StorageClass 是否正常",
            "kubectl describe pod 查看 FailedMount 的具体报错（超时/权限/不存在）",
            "云盘类存储确认挂载点配额（单节点挂载盘数上限）",
            "必要时重建 PVC 或换可用区一致的 StorageClass",
        ],
    ),
    "k8s-evicted": (
        "{target} 被节点驱逐：节点资源压力触发 kubelet 驱逐，不是业务自身故障",
        [
            "kubectl describe node <node> 看 Conditions 里是 MemoryPressure 还是 DiskPressure",
            "清理节点磁盘（镜像/日志/无主 volume），或扩容节点内存",
            "为关键业务设置合理 request/limit 与优先级（PriorityClass）降低被驱逐概率",
            "排查是否有 Pod 无 limit 导致把节点吃满",
        ],
    ),
    "k8s-probe-unhealthy": (
        "{target} 健康检查持续失败：liveness/readiness 探针未通过",
        [
            "区分 liveness 与 readiness：readiness 失败只是摘流量，liveness 失败会重启容器",
            "确认探针路径/端口/协议是否与实际监听一致（HTTPS 探针常见坑）",
            "应用启动慢则加 initialDelaySeconds / startupProbe，不要一味调大 timeout",
            "探针失败常伴随依赖不可用，需同时看下游与数据库连通性",
        ],
    ),
    "k8s-node-not-ready": (
        "节点 {target} 失联：kubelet 停止上报心跳，节点上的 Pod 已不可调度/不可达",
        [
            "kubectl describe node <node> 看 Ready 的 Reason/Message（KubeletStopped、NetworkUnavailable 等）",
            "登上节点确认 kubelet 与容器运行时状态（systemctl status kubelet / containerd）",
            "确认节点资源是否耗尽（磁盘写满、内存打满会导致 kubelet 假死）",
            "节点确实宕机则先摘除（cordon/drain），把业务迁到健康节点",
        ],
    ),
    "k8s-quota": (
        "{target} 触达配额或磁盘上限：资源用尽导致创建/写入失败",
        [
            "kubectl describe quota -n <ns> 看是哪一项配额用尽",
            "kubectl describe node 看 DiskPressure 与 ephemeral-storage 使用",
            "清理无用镜像与日志（crictl rmi --prune、日志轮转）",
            "长期：提高 ResourceQuota 或扩容节点磁盘",
        ],
    ),
    # 指标类（metric = 指标名）
    "k8s_pod_restarts": (
        "{target} Pod 反复重启（累计 {value} 次）：容器没有稳定运行，服务能力在反复中断",
        [
            "kubectl describe pod <pod> 看 Last State / Events 的退出原因",
            "kubectl logs <pod> --previous 取崩溃前的日志",
            "确认是否为探针误杀（liveness 阈值过严）而非真实崩溃",
            "若为发布引入，直接回滚上一版本",
        ],
    ),
    "k8s_deployment_ready_gap": (
        "{target} 副本未就绪：期望副本数与实际就绪数存在缺口，服务冗余下降",
        [
            "kubectl get pods -l <selector> 找出未就绪的 Pod 及其状态",
            "kubectl describe deployment 看 ReplicaFailure 条件与事件",
            "确认是调度不上、镜像拉不到还是启动后探针不过",
            "缺口期间服务容量下降，注意限流与降级",
        ],
    ),
    "k8s_node_ready": (
        "节点 {target} Ready 条件为 False：节点已不可用",
        [
            "kubectl describe node <node> 查看 Ready 的 Reason 与最后心跳时间",
            "确认 kubelet、容器运行时、网络插件（CNI）是否正常",
            "节点资源耗尽（磁盘/内存/PID）也会让 kubelet 失联",
            "先 cordon 摘除，把 Pod 调度到健康节点",
        ],
    ),
    "k8s_pod_cpu_percent": (
        "{target} CPU 用量接近 limit：随时触发 CPU 限流（throttling），延迟会明显抖动",
        [
            "kubectl top pod 对比 request/limit，确认是否长期贴 limit",
            "CPU 限流会直接抬高 P99：先提高 limit 或加副本止血",
            "排查热点线程/死循环（top -Hp、pprof）",
            "长期按真实用量重设 request/limit，避免 request 过小导致调度过载",
        ],
    ),
    "k8s_pod_memory_percent": (
        "{target} 内存用量接近 limit：再涨就会被 OOMKilled",
        [
            "确认是缓慢增长（泄漏）还是阶梯上涨（缓存/批量任务）",
            "临时抬高 limit 止血，同时导堆或采集 profile 找增长点",
            "JVM 确认 -Xmx 与容器 limit 的比例，预留堆外与元空间",
            "检查是否有无上限缓存、大结果集一次性加载",
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
        # 结论与步骤里的 {target} / {value} 替换成真实对象与数值，避免出现别处的服务名
        target_text = ctx.target or ctx.anomaly.target
        raw_value = ctx.anomaly.value
        value_text = f"{raw_value:g}" if raw_value is not None else "-"

        def _fill(text: str) -> str:
            return text.replace("{target}", target_text).replace("{value}", value_text)

        root_cause = _fill(template)
        suggestions = [_fill(step) for step in steps]
        evidence: list[str] = [f"异常类型命中规则：{ctx.anomaly.rule_name or ctx.metric}"]

        topo = getattr(ctx, "topology", None) or {}
        if topo.get("available"):
            upstream = topo.get("upstream") or []
            downstream = topo.get("downstream") or []
            descendants = topo.get("descendants") or []
            if upstream:
                evidence.append(f"拓扑：{ctx.target} 的上游为 {'、'.join(upstream)}")
            else:
                evidence.append(f"拓扑：{ctx.target} 是入口服务，没有上游依赖")
            if descendants:
                evidence.append(
                    f"拓扑：{ctx.target} 的下游链路为 {'、'.join(descendants)}，"
                    "它们若同时异常应优先怀疑本对象"
                )
            elif downstream:
                evidence.append(f"拓扑：{ctx.target} 的直接下游为 {'、'.join(downstream)}")

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
        topo = getattr(ctx, "topology", None) or {}
        if topo.get("available"):
            lines.append(
                f"服务拓扑（来源 {topo.get('source', '-')}）："
                f"上游={topo.get('upstream') or '无（入口服务）'}；"
                f"下游={topo.get('downstream') or '无（末端服务）'}；"
                f"全链路可达下游={topo.get('descendants') or '无'}"
            )
            lines.append(
                "判断要求：若上游与下游在同一时间窗内同时异常，根因应落在更上游的那个对象；"
                "下游的异常视为连带影响，不要各自归因。"
            )
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
