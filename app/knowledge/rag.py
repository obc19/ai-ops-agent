"""RAG 检索：只检索 confirmed 状态的经验，注入提示词辅助 Agent。"""

from __future__ import annotations

from typing import Any

from app.config.settings import get_settings
from app.knowledge.vector_store import VectorStore, get_vector_store
from app.llm.client import LLMError, embed_texts
from app.models.diagnosis import RAGHit
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RAGService:
    def __init__(self, store: VectorStore | None = None) -> None:
        self.settings = get_settings()
        self.store = store or get_vector_store()

    async def retrieve(self, query: str, top_k: int | None = None) -> list[RAGHit]:
        top_k = top_k or self.settings.knowledge_top_k
        if not query.strip() or self.store.count() == 0:
            return []
        try:
            vectors, _source = await embed_texts([query])
        except LLMError as exc:
            logger.warning("RAG 向量化失败，跳过检索：%s", exc)
            return []
        hits = self.store.search(vectors[0], top_k=top_k)
        results: list[RAGHit] = []
        for item_id, score, payload in hits:
            if score < self.settings.rag_min_score:
                continue
            results.append(
                RAGHit(
                    experience_id=item_id,
                    title=str(payload.get("title", "")),
                    score=score,
                    root_cause=str(payload.get("root_cause", "")),
                    resolution=[str(x) for x in payload.get("resolution", [])],
                )
            )
        if results:
            logger.info("RAG 命中 %d 条历史经验（最高 %.3f）", len(results), results[0].score)
        return results

    @staticmethod
    def build_prompt_block(hits: list[RAGHit]) -> str:
        """把命中经验拼成提示词片段，供 LLM 参考。"""
        if not hits:
            return ""
        lines = ["以下是历史相似故障经验（供参考，勿照搬）："]
        for idx, hit in enumerate(hits, 1):
            steps = "；".join(hit.resolution[:4])
            lines.append(f"{idx}. [{hit.score:.2f}] {hit.title} — 根因：{hit.root_cause}；处置：{steps}")
        return "\n".join(lines)


def payload_from_experience(exp: Any) -> dict[str, Any]:
    return {
        "title": exp.title,
        "target": getattr(exp, "target", ""),
        "root_cause": exp.root_cause,
        "resolution": exp.resolution,
        "tags": exp.tags,
        "confidence": exp.confidence,
    }
