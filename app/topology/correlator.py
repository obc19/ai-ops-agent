"""异常关联收敛：把同一时刻的多条异常归并成「1 个源头 + N 个受影响」。

为什么需要这一步：没有它，5 个服务同时异常就产生 5 条独立告警、5 份独立报告，
每份都在猜自己的根因，而人还是得自己去判断「其实是 payment 挂了把 order 拖垮」。
AIOps 的核心价值恰恰是把 N 收敛成 1。

算法（沿拓扑序传播归属，O(V+E)）：

    按拓扑序（上游 → 下游）遍历异常节点 t：
        若 t 的任一「直接上游」已经归属某个群 → t 归入该群（它是被带垮的）
        否则 → t 自己新建一个群（它是源头）

这样天然满足：
    gateway → order → payment 全异常      ⇒ 1 个群，根因 gateway
    order → payment 异常、gateway 正常     ⇒ 1 个群，根因 order
    order → payment 与 order → user 异常   ⇒ 1 个群，根因 order，影响 2 个下游
    两个互不相关的服务同时异常             ⇒ 2 个群，各自独立

没有拓扑数据时**不猜**：每个异常独立成群，并在 reason 里写明「未做关联」。
宁可少收敛，也不能把无关故障硬并成一个——那会让运维漏掉真实的第二起故障。
"""

from __future__ import annotations

from collections import defaultdict

from app.models.enums import Severity
from app.models.events import AnomalyEvent
from app.models.topology import AnomalyCluster, ServiceGraph

# P0 最严重，用于「影响面提升根因等级」时取最大值
_SEVERITY_RANK = {Severity.P0: 0, Severity.P1: 1, Severity.P2: 2}


def _worst(items: list[AnomalyEvent]) -> AnomalyEvent:
    """取最严重的一条作为代表；同级取先出现的，保证结果稳定。"""
    return min(items, key=lambda a: _SEVERITY_RANK.get(a.severity, 9))


def _worse(a: Severity, b: Severity) -> Severity:
    return a if _SEVERITY_RANK.get(a, 9) <= _SEVERITY_RANK.get(b, 9) else b


def correlate(
    anomalies: list[AnomalyEvent],
    graph: ServiceGraph,
    escalate_by_impact: bool = True,
) -> list[AnomalyCluster]:
    """把异常列表收敛成若干同源群。返回顺序按根因严重度降序。"""
    if not anomalies:
        return []

    # ---- 1. 按 target 归并：同一对象的多个指标异常属于同一个「异常节点」----
    by_target: dict[str, list[AnomalyEvent]] = defaultdict(list)
    for anomaly in anomalies:
        by_target[anomaly.target or "unknown"].append(anomaly)

    representatives: dict[str, AnomalyEvent] = {
        target: _worst(items) for target, items in by_target.items()
    }

    # ---- 2. 无拓扑可用时，安全退化：不猜，一个一个独立成案 ----
    if graph.is_empty():
        return [
            AnomalyCluster(
                root=root,
                members=[m for m in by_target[target] if m.id != root.id],
                reason="无拓扑数据，未做关联（每条异常独立诊断）",
                impact=[],
                topology_used=False,
            )
            for target, root in sorted(
                representatives.items(), key=lambda kv: _SEVERITY_RANK.get(kv[1].severity, 9)
            )
        ]

    # ---- 3. 沿拓扑序传播归属 ----
    # owner: target → 该群根因的 target
    owner: dict[str, str] = {}
    groups: dict[str, list[str]] = {}  # 根因 target → 群内 target 列表

    ordered = graph.topological_order()
    known = set(ordered)
    # 不在图上的异常（例如主机 node-1）排在最后，各自独立
    ordered_targets = [t for t in ordered if t in representatives]
    ordered_targets += [t for t in representatives if t not in known]

    for target in ordered_targets:
        if target not in known:
            # 不在图上：无法判断上下游，自己当源头
            owner[target] = target
            groups[target] = [target]
            continue

        # 只看直接上游：更远的上游会通过链式传播自然归到同一个群
        upstream_bad = [up for up in graph.upstream_of(target) if up in owner]
        if upstream_bad:
            # 归到「最上游那个异常直接上游」所在的群
            root_target = min(
                (owner[up] for up in upstream_bad),
                key=lambda t: _order_index(ordered, t),
            )
            owner[target] = root_target
            groups.setdefault(root_target, [root_target]).append(target)
        else:
            owner[target] = target
            groups.setdefault(target, [target])

    # ---- 4. 组装结果 ----
    clusters: list[AnomalyCluster] = []
    for root_target, targets in groups.items():
        root = representatives[root_target]
        members: list[AnomalyEvent] = []
        for target in targets:
            for anomaly in by_target[target]:
                if anomaly.id != root.id:
                    members.append(anomaly)

        affected = [t for t in targets if t != root_target]
        reachable = [d for d in graph.descendants(root_target) if d in representatives]

        if affected:
            reason = (
                f"{root_target} 是 {', '.join(affected)} 的上游依赖，"
                f"其异常导致下游连带异常，判定为源头"
            )
        elif reachable:
            reason = f"{root_target} 自身异常，下游 {', '.join(reachable)} 亦受影响但未达告警阈值"
        else:
            reason = f"{root_target} 独立异常，拓扑上无关联对象同时异常"

        if escalate_by_impact and members:
            worst_member = _worst(members)
            merged = _worse(root.severity, worst_member.severity)
            if merged is not root.severity:
                root = root.model_copy(
                    update={
                        "severity": merged,
                        "summary": f"{root.summary}（因影响 {len(affected)} 个下游，等级由 "
                        f"{representatives[root_target].severity.value} 提升至 {merged.value}）",
                    }
                )

        clusters.append(
            AnomalyCluster(
                root=root,
                members=members,
                reason=reason,
                impact=affected or reachable,
                topology_used=True,
            )
        )

    clusters.sort(
        key=lambda c: (
            _SEVERITY_RANK.get(c.root.severity, 9),
            -len(c.members),
            c.root.target,
        )
    )
    return clusters


def _order_index(order: list[str], target: str) -> int:
    try:
        return order.index(target)
    except ValueError:
        return len(order)
