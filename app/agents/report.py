"""诊断报告渲染：对外统一 Markdown（确认书 D11）。"""

from __future__ import annotations

from app.models.diagnosis import DiagnosisReport
from app.models.events import AnomalyEvent
from app.utils.clock import fmt_local

STATUS_LABEL = {"ok": "完整", "partial": "部分降级", "failed": "失败"}


def render_markdown(report: DiagnosisReport, anomaly: AnomalyEvent | None = None) -> str:
    lines: list[str] = [
        f"# 故障诊断报告 · {report.target}",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 报告 ID | `{report.id}` |",
        f"| 异常 ID | `{report.anomaly_id}` |",
        f"| 对象 | {report.cluster} / {report.namespace} / {report.target} |",
        f"| 状态 | {STATUS_LABEL.get(report.status.value, report.status.value)} |",
        f"| 置信度 | {report.confidence:.2f} |",
        f"| 诊断耗时 | {report.duration_ms / 1000:.2f} 秒 |",
        f"| 生成时间 | {fmt_local(report.created_at)} |",
    ]
    if anomaly is not None:
        lines.extend(
            [
                f"| 触发指标 | {anomaly.metric} = {anomaly.value}（阈值 {anomaly.threshold}） |",
                f"| 严重级别 | {anomaly.severity.value} |",
                f"| 时间窗口 | {anomaly.window.label()} |",
                f"| 触发规则 | {anomaly.rule_name or anomaly.rule_id} |",
            ]
        )
    lines.append("")

    lines.extend(["## 根因判断", "", report.root_cause or "（未得出明确结论）", ""])

    if report.degraded_agents:
        lines.extend(
            [
                "> **注意**：以下维度降级或未参与，结论置信度已下调："
                + "、".join(report.degraded_agents),
                "",
            ]
        )

    lines.extend(["## 各维度分析", "", "| 维度 | 置信度 | 结论 | 状态 |", "| --- | --- | --- | --- |"])
    for fragment in report.fragments:
        state = "降级" if fragment.degraded else "正常"
        lines.append(
            f"| {fragment.agent.value} | {fragment.confidence:.2f} | "
            f"{fragment.conclusion or '-'} | {state} |"
        )
    lines.append("")

    for fragment in report.fragments:
        if not fragment.evidence:
            continue
        lines.append(f"### {fragment.agent.value} 依据")
        lines.append("")
        lines.extend(f"- {item}" for item in fragment.evidence)
        if fragment.skip_reason:
            lines.append(f"- 跳过原因：{fragment.skip_reason}")
        lines.append("")

    if report.rag_hits:
        lines.extend(["## 历史相似经验", ""])
        for hit in report.rag_hits:
            steps = "；".join(hit.resolution[:3]) or "-"
            lines.append(f"- **{hit.title}**（相似度 {hit.score:.2f}）根因：{hit.root_cause}；处置：{steps}")
        lines.append("")

    lines.extend(["## 处置建议", ""])
    if report.suggestion:
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(report.suggestion, 1))
    else:
        lines.append("暂无建议，请人工复核。")
    lines.append("")

    lines.extend(
        [
            "---",
            "",
            f"*由 ai-ops-agent 生成｜LLM 参与：{'是' if report.used_llm else '否'}"
            f"｜数据已过脱敏网关*",
        ]
    )
    return "\n".join(lines)
