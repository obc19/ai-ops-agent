"""经验知识库接口：草稿 → 确认 → 回滚 → 检索。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import require_auth
from app.knowledge.experience import DuplicateExperience, KnowledgeError, get_knowledge_service
from app.models.knowledge import Experience, KnowledgeStats
from app.storage import get_store

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DraftRequest(BaseModel):
    diagnosis_id: str
    tags: list[str] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    confirmed_by: str
    overrides: dict | None = None
    force: bool = False


class RollbackRequest(BaseModel):
    reason: str
    actor: str = "operator"


@router.get("", response_model=list[Experience], summary="经验列表")
async def list_experiences(
    status: str | None = Query(default=None, description="draft / confirmed / rolled_back"),
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_auth),
) -> list[Experience]:
    return get_knowledge_service().list(status=status, limit=limit)


@router.get("/stats", response_model=KnowledgeStats, summary="知识库统计")
async def stats(actor: str = Depends(require_auth)) -> KnowledgeStats:
    return get_knowledge_service().stats()


@router.get("/search", summary="按语义检索经验")
async def search(
    q: str = Query(min_length=1), top_k: int = Query(default=3, ge=1, le=20),
    actor: str = Depends(require_auth),
) -> dict:
    service = get_knowledge_service()
    hits = await service.rag.retrieve(q, top_k=top_k)
    return {"query": q, "hits": [h.model_dump() for h in hits]}


@router.post("", response_model=Experience, summary="由诊断报告生成经验草稿")
async def create_draft(payload: DraftRequest, actor: str = Depends(require_auth)) -> Experience:
    report = get_store().get_diagnosis(payload.diagnosis_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"诊断报告不存在：{payload.diagnosis_id}")
    return get_knowledge_service().create_draft(report, tags=payload.tags)


@router.post("/{exp_id}/confirm", response_model=Experience, summary="人工确认入库")
async def confirm(
    exp_id: str, payload: ConfirmRequest, actor: str = Depends(require_auth)
) -> Experience:
    if not payload.confirmed_by.strip():
        raise HTTPException(status_code=422, detail="confirmed_by 必填：经验入库必须人工确认")
    try:
        result = await get_knowledge_service().confirm(
            exp_id, payload.confirmed_by, payload.overrides, payload.force
        )
    except DuplicateExperience as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.experience


@router.post("/{exp_id}/rollback", response_model=Experience, summary="回滚经验")
async def rollback(
    exp_id: str, payload: RollbackRequest, actor: str = Depends(require_auth)
) -> Experience:
    try:
        return get_knowledge_service().rollback(exp_id, payload.reason, payload.actor or actor)
    except KnowledgeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{exp_id}", response_model=Experience, summary="经验详情")
async def get_experience(exp_id: str, actor: str = Depends(require_auth)) -> Experience:
    exp = get_knowledge_service().get(exp_id)
    if exp is None:
        raise HTTPException(status_code=404, detail=f"经验不存在：{exp_id}")
    return exp
