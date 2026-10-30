"""编排中枢：按需调度、超时控制、失败降级、结果聚合与 Markdown 渲染。

骨架阶段用 asyncio 组合实现状态机；接口与 LangGraph 等价，
后续需要复杂分支时替换为 LangGraph 图即可，Agent 无需改动。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.agents.base import BaseAgent, DiagnosisContext
from app.agents.log_agent import LogAgent
from app.agents.metric_agent import MetricAgent
from app.agents.report import render_markdown
from app.agents.root_cause_agent import RootCauseAgent
from app.agents.trace_agent import TraceAgent
from app.collectors import collect_all
from app.config.settings import get_settings
from app.knowledge.rag import RAGService
from app.models.diagnosis import DiagnosisFragment, DiagnosisReport
from app.models.enums import AgentKind, DiagnosisStatus
from app.models.events import (
    AnomalyEvent,
    K8sEvent,
    LogEvent,
    MetricEvent,
    TimeWindow,
    TraceEvent,
)
from app.storage import get_store
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)

METRIC_WEIGHT = 0.45
LOG_WEIGHT = 0.35
TRACE_WEIGHT = 0.2


class Orchestrator:
    def __init__(self, store=None) -> None:
        self.settings = get_settings()
        self.db = store or get_store()
        self.rag = RAGService()
        self.agents: list[BaseAgent] = [MetricAgent(), LogAgent(), TraceAgent()]
        self.root_agent = RootCauseAgent()

    # ---------- 主流程 ----------
    async def run(
        self,
        anomaly: AnomalyEvent,
        window: TimeWindow | None = None,
        correlation=None,
    ) -> DiagnosisReport:
        started = utc_now()
        window = window or anomaly.window
        logger.info("编排开始：%s", anomaly.display())
        self.db.save_anomaly(anomaly)

        events = await collect_all(window)
        # 先取拓扑：根因 Agent 需要用上下游关系做因果推断（谁带垮了谁）
        topology_info = await self._topology_info(anomaly.target)
        ctx = DiagnosisContext(
            anomaly=anomaly,
            window=window,
            metric_events=[e for e in events if isinstance(e, MetricEvent)],
            log_events=[e for e in events if isinstance(e, LogEvent)],
            trace_events=[e for e in events if isinstance(e, TraceEvent)],
            k8s_events=[e for e in events if isinstance(e, K8sEvent)],
            store=self.db,
            topology=topology_info,
        )
        ctx.rag_hits = await self.rag.retrieve(self._rag_query(anomaly))

        fragments = await self._run_agents(ctx)
        root = await self.root_agent.synthesize(ctx, fragments)
        fragments.append(root.fragment)

        degraded = [f.agent.value for f in fragments if f.degraded]
        active = [f for f in fragments if f.agent is not AgentKind.ROOT_CAUSE and not f.degraded]
        if not active:
            status = DiagnosisStatus.FAILED
        elif degraded:
            status = DiagnosisStatus.PARTIAL
        else:
            status = DiagnosisStatus.OK

        report = DiagnosisReport(
            anomaly_id=anomaly.id,
            status=status,
            cluster=anomaly.cluster,
            namespace=anomaly.namespace,
            target=anomaly.target,
            summary=anomaly.summary,
            root_cause=root.root_cause,
            confidence=root.fragment.confidence,
            suggestion=root.suggestion,
            fragments=fragments,
            rag_hits=ctx.rag_hits,
            used_llm=root.used_llm,
            degraded_agents=degraded,
            correlation=correlation.to_public() if correlation is not None else {},
            topology=topology_info,
            created_at=started,
        )
        if len(active) < 3:
            report.confidence = report.confidence_with_penalty(3 - len(active), 3)

        report.duration_ms = round((utc_now() - started).total_seconds() * 1000, 1)
        report.markdown = render_markdown(report, anomaly)
        self.db.save_diagnosis(report)
        self.db.append_audit(
            "diagnosis_completed",
            {
                "report_id": report.id,
                "status": status.value,
                "confidence": report.confidence,
                "duration_ms": report.duration_ms,
                "used_llm": report.used_llm,
                "rag_hits": len(ctx.rag_hits),
                "correlated_suppressed": len(correlation.members) if correlation else 0,
                "topology_source": topology_info.get("source", ""),
            },
            target=anomaly.target,
        )
        logger.info(
            "编排完成：%s 状态=%s 置信度=%.2f 耗时=%.2fs",
            report.id,
            status.value,
            report.confidence,
            report.duration_ms / 1000,
        )
        return report

    async def _topology_info(self, target: str) -> dict:
        """取该对象在依赖图中的位置，供报告展示与根因表述使用。"""
        try:
            from app.topology import get_topology

            graph = await get_topology().current()
        except Exception as exc:  # noqa: BLE001 - 拓扑不可用不得阻断诊断
            logger.warning("读取拓扑失败：%s", exc)
            return {"available": False, "note": f"拓扑不可用：{exc}"}

        if graph.is_empty():
            return {
                "available": False,
                "note": "未获得服务依赖数据（未配置 JAEGER_URL，且未声明 topology.dependencies）",
            }
        if not graph.has_node(target):
            return {
                "available": False,
                "note": f"{target} 不在服务依赖图中（主机、拨测目标等非服务对象）",
                "source": graph.source,
            }
        return {
            "available": True,
            "source": graph.source,
            "upstream": graph.upstream_of(target),
            "downstream": graph.downstream_of(target),
            "ancestors": graph.ancestors(target),
            "descendants": graph.descendants(target),
            "path": graph.render_ascii(),
        }

    # ---------- 调度 ----------
    async def _run_agents(self, ctx: DiagnosisContext) -> list[DiagnosisFragment]:
        timeout = self.settings.agent_timeout
        tasks = [asyncio.create_task(self._guarded(agent, ctx, timeout)) for agent in self.agents]
        fragments = await asyncio.gather(*tasks)
        return list(fragments)

    async def _guarded(
        self, agent: BaseAgent, ctx: DiagnosisContext, timeout: float
    ) -> DiagnosisFragment:
        started = utc_now()
        try:
            fragment = await asyncio.wait_for(agent.analyze(ctx), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("%s 超时（%.1fs），标记降级", agent.label, timeout)
            fragment = DiagnosisFragment(
                agent=agent.kind,
                conclusion="执行超时",
                degraded=True,
                skip_reason=f"超过单 Agent 超时预算 {timeout:.0f}s",
            )
        except Exception as exc:  # noqa: BLE001 - 单维度失败不得阻断全局
            logger.exception("%s 执行异常：%s", agent.label, exc)
            fragment = DiagnosisFragment(
                agent=agent.kind,
                conclusion="执行异常",
                degraded=True,
                skip_reason=str(exc)[:200],
            )
        fragment.elapsed_ms = round((utc_now() - started).total_seconds() * 1000, 1)
        return fragment

    @staticmethod
    def _rag_query(anomaly: AnomalyEvent) -> str:
        return " ".join(
            part
            for part in [
                anomaly.target,
                anomaly.metric,
                anomaly.rule_name,
                anomaly.summary,
            ]
            if part
        )

    # ---------- 便捷入口 ----------
    async def diagnose_anomaly(self, anomaly_id: str) -> DiagnosisReport | None:
        for item in self.db.list_anomalies(limit=200):
            if item.id == anomaly_id:
                return await self.run(item)
        return None

    async def diagnose_all(self, window: TimeWindow | None = None) -> list[DiagnosisReport]:
        """采集 + 检测 + 逐个诊断，供轮询调度使用。"""
        from app.detection import get_engine

        window = window or TimeWindow(
            start=utc_now() - timedelta(minutes=5), end=utc_now()
        )
        events = await collect_all(window)
        anomalies = get_engine().detect(events, window)
        reports: list[DiagnosisReport] = []
        for anomaly in anomalies:
            reports.append(await self.run(anomaly, window))
        return reports
