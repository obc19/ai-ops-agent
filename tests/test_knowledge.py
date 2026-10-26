"""知识库测试：草稿 → 确认 → 检索 → 回滚。"""

from __future__ import annotations

import asyncio

import pytest

from app.knowledge.experience import DuplicateExperience, KnowledgeError, get_knowledge_service
from app.models import DiagnosisReport


def _report(target: str = "order-service") -> DiagnosisReport:
    return DiagnosisReport(
        anomaly_id="ano-test",
        target=target,
        summary=f"{target} CPU 使用率 94% 严重超限",
        root_cause="单实例 CPU 打满，线程池耗尽导致请求排队",
        confidence=0.82,
        suggestion=["扩容副本至 4", "排查死循环任务"],
    )


def test_draft_requires_human_confirm(settings):
    service = get_knowledge_service()
    exp = service.create_draft(_report())
    assert exp.status.value == "draft"
    with pytest.raises(KnowledgeError):
        asyncio.run(service.confirm(exp.id, "  "))


def test_confirm_and_retrieve(settings):
    service = get_knowledge_service()
    exp = service.create_draft(_report("payment-service"))
    result = asyncio.run(service.confirm(exp.id, "alice"))
    assert result.experience.status.value == "confirmed"
    assert result.experience.embedding
    hits = asyncio.run(service.rag.retrieve("payment-service CPU 打满 线程池耗尽"))
    assert any(h.experience_id == exp.id for h in hits)


def test_duplicate_rejected(settings):
    service = get_knowledge_service()
    first = service.create_draft(_report("user-service"))
    asyncio.run(service.confirm(first.id, "alice"))

    second = service.create_draft(_report("user-service"))
    with pytest.raises(DuplicateExperience):
        asyncio.run(service.confirm(second.id, "bob"))

    forced = asyncio.run(service.confirm(second.id, "bob", force=True))
    assert forced.experience.status.value == "confirmed"


def test_rollback_removes_from_index(settings):
    service = get_knowledge_service()
    exp = service.create_draft(_report("search-service"))
    asyncio.run(service.confirm(exp.id, "alice"))
    service.rollback(exp.id, "误入库")
    hits = asyncio.run(service.rag.retrieve("search-service CPU 打满"))
    assert all(h.experience_id != exp.id for h in hits)
