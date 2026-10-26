"""日志诊断 Agent：检索异常时段日志，提取关键错误线索。"""

from __future__ import annotations

import re
from collections import Counter

from app.agents.base import BaseAgent, DiagnosisContext
from app.models.diagnosis import DiagnosisFragment
from app.models.enums import AgentKind
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 错误模式 → 语义线索
PATTERN_HINTS = [
    (r"OutOfMemoryError|heap space|OOMKilled", "内存耗尽"),
    (r"timeout|deadline exceeded|Timed out", "下游调用超时"),
    (r"connection refused|Connection reset", "下游连接被拒"),
    (r"no space left on device", "磁盘空间不足"),
    (r"worker pool exhausted|queue length|throttl", "线程池耗尽 / 被 CPU 限流"),
    (r"HTTP 5\d\d|Status 5\d\d", "服务端返回 5xx"),
]


class LogAgent(BaseAgent):
    kind = AgentKind.LOG
    label = "日志诊断"
    weight = 0.35

    async def analyze(self, ctx: DiagnosisContext) -> DiagnosisFragment:
        logs = ctx.logs_for()
        if not logs:
            return self.skip("窗口内没有 ERROR/WARN 级日志")

        total = sum(item.count for item in logs)
        hints: Counter[str] = Counter()
        for item in logs:
            for pattern, hint in PATTERN_HINTS:
                if re.search(pattern, item.message, re.IGNORECASE):
                    hints[hint] += item.count
                    break

        top_messages = [
            f"{item.level} ×{item.count}：{item.message[:90]}"
            for item in sorted(logs, key=lambda x: x.count, reverse=True)[:3]
        ]
        evidence = [f"共检索到 {len(logs)} 条异常日志（累计 {total} 次）"]
        evidence.extend(top_messages)
        if hints:
            evidence.append("线索归类：" + "、".join(f"{k}×{v}" for k, v in hints.most_common(3)))

        main_hint = hints.most_common(1)[0][0] if hints else "日志异常"
        error_count = sum(item.count for item in logs if item.level.upper() == "ERROR")
        confidence = 0.4 + min(total / 30, 1.0) * 0.25 + (0.1 if error_count else 0.0)

        return DiagnosisFragment(
            agent=self.kind,
            confidence=round(min(confidence, 0.88), 3),
            conclusion=f"日志指向「{main_hint}」，共 {total} 次相关记录",
            evidence=evidence,
        )
