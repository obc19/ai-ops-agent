"""拓扑构建：把原始调用关系整理成 ServiceGraph。

输入可以是三种形态，优先级从高到低：
1. 链路数据推导出的 (caller, callee) 边；
2. 配置里手工声明的 `topology.dependencies`；
3. 都没有 → 空图（关联收敛会自动退化为「不收敛」，不会误判）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.models.topology import ServiceGraph, TopologyEdge


def build_graph(
    edges: Iterable[tuple[str, str]] | Iterable[TopologyEdge],
    source: str = "trace",
    note: str = "",
    extra_nodes: Iterable[str] = (),
) -> ServiceGraph:
    """合并重复边、丢弃自环，产出不可变快照。

    自环（A → A）不是服务依赖，是应用内部调用，必须剔除，
    否则拓扑排序会把它当环、`upstream_of(A)` 会把 A 自己算进去。
    """
    merged: dict[tuple[str, str], TopologyEdge] = {}
    for item in edges:
        if isinstance(item, TopologyEdge):
            caller, callee = item.caller, item.callee
            calls, errors, p99 = item.calls, item.error_calls, item.p99_ms
        else:
            caller, callee = item[0], item[1]
            calls, errors, p99 = 1, 0, 0.0
        caller, callee = caller.strip(), callee.strip()
        if not caller or not callee or caller == callee:
            continue
        key = (caller, callee)
        if key in merged:
            merged[key].calls += max(calls, 1)
            merged[key].error_calls += errors
            merged[key].p99_ms = max(merged[key].p99_ms, p99)
        else:
            merged[key] = TopologyEdge(
                caller=caller,
                callee=callee,
                calls=max(calls, 1),
                error_calls=errors,
                p99_ms=p99,
            )

    nodes: set[str] = set(extra_nodes)
    for edge in merged.values():
        nodes.add(edge.caller)
        nodes.add(edge.callee)

    return ServiceGraph(
        nodes=sorted(nodes),
        edges=sorted(merged.values(), key=lambda e: (e.caller, e.callee)),
        source=source,
        note=note,
    )


def parse_manual(dependencies: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """解析配置里的手工拓扑。

    支持两种写法，都为了少打错字：

    ```yaml
    dependencies:
      order-service: [payment-service, user-service]
    # 或
    dependencies:
      - caller: order-service
        callee: payment-service
    ```
    """
    if not dependencies:
        return []
    edges: list[tuple[str, str]] = []
    if isinstance(dependencies, Mapping):
        for caller, callees in dependencies.items():
            if isinstance(callees, str):
                edges.append((str(caller), callees))
            elif isinstance(callees, (list, tuple)):
                for callee in callees:
                    if callee:
                        edges.append((str(caller), str(callee)))
        return edges
    if isinstance(dependencies, (list, tuple)):
        for item in dependencies:
            if isinstance(item, Mapping):
                caller = item.get("caller") or item.get("from")
                callee = item.get("callee") or item.get("to")
                if caller and callee:
                    edges.append((str(caller), str(callee)))
    return edges
