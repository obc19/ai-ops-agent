"""诊断模型：Agent 片段、RAG 命中、最终报告。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import AgentKind, DiagnosisStatus
from app.utils.clock import utc_now
from app.utils.ids import new_id


class DiagnosisFragment(BaseModel):
    """单个维度 Agent 的结构化产出，必须带依据与置信度。"""

    agent: AgentKind
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    conclusion: str = ""
    evidence: list[str] = Field(default_factory=list)
    degraded: bool = False
    skip_reason: str | None = None
    elapsed_ms: float = 0.0

    def brief(self) -> str:
        state = "降级" if self.degraded else "正常"
        return f"{self.agent.value}: 置信度 {self.confidence:.2f}（{state}）— {self.conclusion}"


class RAGHit(BaseModel):
    experience_id: str
    title: str
    score: float
    root_cause: str = ""
    resolution: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("diag"))
    anomaly_id: str = ""
    status: DiagnosisStatus = DiagnosisStatus.OK
    cluster: str = "default"
    namespace: str = "default"
    target: str = ""
    summary: str = ""
    root_cause: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    suggestion: list[str] = Field(default_factory=list)
    fragments: list[DiagnosisFragment] = Field(default_factory=list)
    rag_hits: list[RAGHit] = Field(default_factory=list)
    used_llm: bool = False
    degraded_agents: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    markdown: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    def confidence_with_penalty(self, missing: int, total: int) -> float:
        """缺失维度按比例惩罚置信度，最低保留 0.3。"""
        if total <= 0:
            return self.confidence
        penalty = 1.0 - (missing / total) * 0.6
        return max(round(self.confidence * penalty, 3), 0.3)
