"""服务依赖拓扑模型：由链路数据推导出的有向调用图。

这张图是「根因收敛」的唯一依据——没有它，多条同时发生的异常只能各自成案，
有了它才能判定「谁是源头、谁是被带垮的」。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from pydantic import BaseModel, Field

from app.utils.clock import iso_utc, utc_now


class TopologyEdge(BaseModel):
    """一条调用边：caller 调用 callee。"""

    caller: str
    callee: str
    calls: int = 0
    error_calls: int = 0
    p99_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        return round(self.error_calls / self.calls * 100, 1) if self.calls else 0.0

    def display(self) -> str:
        return f"{self.caller} → {self.callee}"

    def to_public(self) -> dict:
        return {
            "caller": self.caller,
            "callee": self.callee,
            "calls": self.calls,
            "error_rate": self.error_rate,
            "p99_ms": self.p99_ms,
        }


class ServiceGraph(BaseModel):
    """服务依赖图快照。构建后只读。"""

    nodes: list[str] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
    source: str = "trace"  # trace | config | empty
    updated_at: datetime = Field(default_factory=utc_now)
    note: str = ""

    # ---------- 基础查询 ----------
    def _adjacency(self) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for edge in self.edges:
            adj.setdefault(edge.caller, []).append(edge.callee)
        return adj

    def _reverse(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = {n: [] for n in self.nodes}
        for edge in self.edges:
            rev.setdefault(edge.callee, []).append(edge.caller)
        return rev

    def has_node(self, name: str) -> bool:
        return name in self.nodes

    def upstream_of(self, name: str) -> list[str]:
        """谁调用我（我的依赖方/上级）。"""
        return sorted(set(self._reverse().get(name, [])))

    def downstream_of(self, name: str) -> list[str]:
        """我调用谁（我的被依赖方/下级）。"""
        return sorted(set(self._adjacency().get(name, [])))

    def roots(self) -> list[str]:
        """入口服务：不被任何人调用。"""
        rev = self._reverse()
        return sorted(n for n in self.nodes if not rev.get(n))

    def leaves(self) -> list[str]:
        """末端服务：不调用任何人。"""
        adj = self._adjacency()
        return sorted(n for n in self.nodes if not adj.get(n))

    def descendants(self, name: str) -> list[str]:
        """沿调用方向可达的全部下游（BFS，去环安全）。"""
        adj = self._adjacency()
        seen: set[str] = set()
        queue = deque(adj.get(name, []))
        while queue:
            cur = queue.popleft()
            if cur in seen or cur == name:
                continue
            seen.add(cur)
            queue.extend(adj.get(cur, []))
        return sorted(seen)

    def ancestors(self, name: str) -> list[str]:
        """沿调用方向可达的全部上游。"""
        rev = self._reverse()
        seen: set[str] = set()
        queue = deque(rev.get(name, []))
        while queue:
            cur = queue.popleft()
            if cur in seen or cur == name:
                continue
            seen.add(cur)
            queue.extend(rev.get(cur, []))
        return sorted(seen)

    # ---------- 拓扑序 ----------
    def topological_order(self) -> list[str]:
        """Kahn 算法。存在环时返回能排出的部分，剩余节点排在末尾并标记。"""
        adj = self._adjacency()
        indeg = {n: 0 for n in self.nodes}
        for edge in self.edges:
            indeg[edge.callee] = indeg.get(edge.callee, 0) + 1
        queue = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: list[str] = []
        while queue:
            cur = queue.popleft()
            order.append(cur)
            for nxt in adj.get(cur, []):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        leftover = [n for n in self.nodes if n not in order]
        return order + sorted(leftover)

    def has_cycle(self) -> bool:
        return self._detect_cycle() is not None

    def find_cycle(self) -> list[str]:
        return self._detect_cycle() or []

    def _detect_cycle(self) -> list[str] | None:
        adj = self._adjacency()
        state: dict[str, int] = {}  # 0=未访问 1=在栈 2=已完成
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            state[node] = 1
            stack.append(node)
            for nxt in adj.get(node, []):
                if state.get(nxt, 0) == 1:
                    idx = stack.index(nxt)
                    return stack[idx:] + [nxt]
                if state.get(nxt, 0) == 0:
                    found = visit(nxt)
                    if found:
                        return found
            state[node] = 2
            stack.pop()
            return None

        for node in self.nodes:
            if state.get(node, 0) == 0:
                found = visit(node)
                if found:
                    return found
        return None

    # ---------- 状态 ----------
    def is_empty(self) -> bool:
        return not self.edges

    def stats(self) -> dict:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "roots": self.roots(),
            "leaves": self.leaves(),
            "has_cycle": self.has_cycle(),
            "source": self.source,
        }

    def to_public(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "edges": [e.to_public() for e in self.edges],
            "roots": self.roots(),
            "leaves": self.leaves(),
            "path": self.render_ascii(),
            "source": self.source,
            "note": self.note,
            "updated_at": iso_utc(self.updated_at),
            "stats": self.stats(),
        }

    def render_ascii(self) -> str:
        """给报告用的极简文本拓扑（沿调用方向缩进展开）。"""
        if self.is_empty():
            return "（暂无拓扑数据）"
        adj = self._adjacency()
        lines: list[str] = []

        def walk(node: str, depth: int, path: set[str]) -> None:
            if depth > 6:
                return
            lines.append("  " * depth + ("└─ " if depth else "") + node)
            for child in sorted(adj.get(node, [])):
                if child in path:
                    lines.append("  " * (depth + 1) + "└─ " + child + "（环）")
                    continue
                walk(child, depth + 1, path | {child})

        for root in self.roots():
            walk(root, 0, {root})
        # 环里的节点不属于任何 root，补一份
        rendered = {line.strip(" └─") for line in lines}
        for node in self.nodes:
            if node not in rendered:
                lines.append(f"（孤立/环内）{node}")
        return "\n".join(lines)


class AnomalyCluster(BaseModel):
    """一组同源异常：1 个源头 + N 个被带垮的。"""

    root: object  # AnomalyEvent，用 object 避免循环导入
    members: list = Field(default_factory=list)
    reason: str = ""
    impact: list[str] = Field(default_factory=list)
    topology_used: bool = False

    @property
    def suppressed(self) -> int:
        return len(self.members)

    def to_public(self) -> dict:
        root = self.root
        return {
            "root": {
                "id": getattr(root, "id", ""),
                "target": getattr(root, "target", ""),
                "metric": getattr(root, "metric", ""),
                "severity": getattr(getattr(root, "severity", None), "value", ""),
            },
            "members": [
                {
                    "id": getattr(m, "id", ""),
                    "target": getattr(m, "target", ""),
                    "metric": getattr(m, "metric", ""),
                    "severity": getattr(getattr(m, "severity", None), "value", ""),
                }
                for m in self.members
            ],
            "reason": self.reason,
            "impact": self.impact,
            "topology_used": self.topology_used,
            "suppressed": self.suppressed,
        }
