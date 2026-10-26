"""经验流转：草稿 → 人工确认 → 入库检索 → 可回滚。

关键规则（确认书 D8 / FR-6）：
- 只有 confirmed 才进向量库参与 RAG；
- 入库前相似度超阈值判重复，返回合并提示而不写入；
- 送外部 embedding 前先脱敏，脱敏失败则拒绝入库（fail-closed）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.gateway.desensitizer import DesensitizerError, get_desensitizer
from app.knowledge.rag import RAGService, payload_from_experience
from app.knowledge.vector_store import VectorStore, get_vector_store
from app.llm.client import LLMError, embed_texts
from app.models.diagnosis import DiagnosisReport
from app.models.knowledge import Experience, ExperienceDraft, KnowledgeStats
from app.models.enums import ExperienceStatus
from app.storage import get_store
from app.utils.clock import utc_now
from app.utils.logging import get_logger

logger = get_logger(__name__)


class KnowledgeError(RuntimeError):
    """知识库操作失败（脱敏失败、向量化失败等）。"""


class DuplicateExperience(KnowledgeError):
    def __init__(self, existing: Experience, score: float) -> None:
        super().__init__(
            f"检测到相似经验「{existing.title}」（相似度 {score:.3f}），"
            "已跳过写入，请人工合并或调整内容后重试"
        )
        self.existing = existing
        self.score = score


@dataclass
class ConfirmResult:
    experience: Experience
    embedding_source: str


class KnowledgeService:
    def __init__(self, store: VectorStore | None = None) -> None:
        self.settings = get_settings()
        self.db = get_store()
        self.vectors = store or get_vector_store()
        self.rag = RAGService(self.vectors)
        self.desensitizer = get_desensitizer()

    # ---------- 草稿 ----------
    def create_draft(self, report: DiagnosisReport, tags: list[str] | None = None) -> Experience:
        draft = ExperienceDraft(
            title=(report.summary or f"{report.target} {report.root_cause}")[:48],
            target=report.target,
            symptom=report.summary,
            root_cause=report.root_cause,
            resolution=list(report.suggestion),
            effect="待人工确认",
            confidence=report.confidence,
            tags=tags or [],
            related_diagnosis_id=report.id,
            related_alert_id=None,
        )
        exp = Experience(
            **draft.model_dump(exclude={"related_diagnosis_id", "related_alert_id"}),
            related_diagnosis_id=report.id,
        )
        self.db.save_experience(exp)
        logger.info("生成经验草稿 %s（%s）", exp.id, exp.title)
        return exp

    # ---------- 确认入库 ----------
    async def confirm(
        self,
        exp_id: str,
        confirmed_by: str,
        overrides: dict | None = None,
        force: bool = False,
    ) -> ConfirmResult:
        exp = self.db.get_experience(exp_id)
        if exp is None:
            raise KnowledgeError(f"经验不存在：{exp_id}")
        if exp.status is ExperienceStatus.ROLLED_BACK:
            raise KnowledgeError("已回滚的经验不可直接确认，请重新生成草稿")
        if not confirmed_by.strip():
            raise KnowledgeError("确认人必填（FR-6.2 强制人工确认）")

        if overrides:
            allowed = {"title", "symptom", "root_cause", "resolution", "effect", "tags", "confidence"}
            for key, value in overrides.items():
                if key in allowed and value not in (None, ""):
                    setattr(exp, key, value)

        # 重复检测（仅比对已确认经验）
        text = exp.searchable_text()
        duplicate = await self._find_duplicate(exp)
        if duplicate and not force:
            existing, score = duplicate
            raise DuplicateExperience(existing, score)

        # 脱敏后再向量化（敏感内容不出境）
        try:
            safe = self.desensitizer.redact(text).text
        except DesensitizerError as exc:
            self.db.append_audit(
                "knowledge_confirm_blocked", {"exp_id": exp_id, "reason": str(exc)[:200]}
            )
            raise KnowledgeError(f"脱敏失败，拒绝入库：{exc}") from exc

        try:
            vectors, source = await embed_texts([safe])
        except LLMError as exc:
            raise KnowledgeError(f"向量化失败，经验未入库：{exc}") from exc

        exp.embedding = vectors[0]
        exp.embedding_source = source
        exp.status = ExperienceStatus.CONFIRMED
        exp.confirmed_by = confirmed_by
        exp.confirmed_at = utc_now()

        self.db.save_experience(exp)
        self.vectors.upsert(exp.id, exp.embedding, payload_from_experience(exp))
        self.db.append_audit(
            "knowledge_confirmed",
            {"exp_id": exp.id, "title": exp.title, "embedding_source": source},
            actor=confirmed_by,
            target=exp.id,
        )
        logger.info("经验已入库 %s（%s）", exp.id, source)
        return ConfirmResult(experience=exp, embedding_source=source)

    # ---------- 回滚 ----------
    def rollback(self, exp_id: str, reason: str, actor: str = "system") -> Experience:
        exp = self.db.get_experience(exp_id)
        if exp is None:
            raise KnowledgeError(f"经验不存在：{exp_id}")
        exp.status = ExperienceStatus.ROLLED_BACK
        exp.rolled_back_at = utc_now()
        exp.rollback_reason = reason
        self.db.save_experience(exp)
        self.vectors.remove(exp.id)
        self.db.append_audit(
            "knowledge_rollback",
            {"exp_id": exp.id, "reason": reason},
            actor=actor,
            target=exp.id,
        )
        logger.warning("经验已回滚 %s：%s", exp.id, reason)
        return exp

    # ---------- 查询 ----------
    async def _find_duplicate(self, exp: Experience) -> tuple[Experience, float] | None:
        """同一对象的相近经验才算重复；跨对象的相似故障应各自留存。"""
        if self.vectors.count() == 0:
            return None
        try:
            vectors, _ = await embed_texts([exp.searchable_text()])
        except LLMError:
            return None
        hits = self.vectors.search(vectors[0], top_k=5)
        for item_id, score, _payload in hits:
            if item_id == exp.id or score < self.settings.knowledge_dedup_threshold:
                continue
            existing = self.db.get_experience(item_id)
            if existing is None or existing.status is not ExperienceStatus.CONFIRMED:
                continue
            if existing.target and exp.target and existing.target != exp.target:
                continue
            return existing, score
        return None

    def get(self, exp_id: str) -> Experience | None:
        return self.db.get_experience(exp_id)

    def list(self, status: str | None = None, limit: int = 100) -> list[Experience]:
        return self.db.list_experiences(status=status, limit=limit)

    def stats(self) -> KnowledgeStats:
        return self.db.knowledge_stats(self.vectors.backend)


_service: KnowledgeService | None = None


def get_knowledge_service() -> KnowledgeService:
    global _service
    if _service is None:
        _service = KnowledgeService()
    return _service
