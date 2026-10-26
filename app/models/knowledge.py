"""知识库经验模型：入库必经人工确认，可回滚。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ExperienceStatus
from app.utils.clock import utc_now
from app.utils.ids import new_id


class ExperienceDraft(BaseModel):
    """诊断完成后自动生成的草稿，等待人工确认。"""

    title: str
    symptom: str
    root_cause: str
    resolution: list[str] = Field(default_factory=list)
    effect: str = ""
    tags: list[str] = Field(default_factory=list)
    related_alert_id: str | None = None
    related_diagnosis_id: str | None = None
    confidence: float = 0.0


class Experience(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    title: str
    symptom: str = ""
    root_cause: str = ""
    resolution: list[str] = Field(default_factory=list)
    effect: str = ""
    confidence: float = 0.0
    confirmed_by: str = ""
    related_alert_id: str | None = None
    related_diagnosis_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    embedding_source: str = ""
    status: ExperienceStatus = ExperienceStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    confirmed_at: datetime | None = None
    rolled_back_at: datetime | None = None
    rollback_reason: str = ""

    def searchable_text(self) -> str:
        """参与向量化的文本，不含确认人等元信息。"""
        parts = [self.title, self.symptom, self.root_cause, " ".join(self.resolution)]
        return "\n".join(p for p in parts if p)


class KnowledgeStats(BaseModel):
    total: int = 0
    draft: int = 0
    confirmed: int = 0
    rolled_back: int = 0
    backend: str = "simple"
