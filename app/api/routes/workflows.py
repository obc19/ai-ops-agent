"""取证工作流：现在加载了哪些、什么时候会触发、能采集什么。

给运维看的，不用去翻 YAML 也能确认配置有没有生效。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_auth
from app.config.settings import get_settings
from app.workflows import list_providers, load_workflows

router = APIRouter(tags=["workflows"])


@router.get("/workflows", summary="取证工作流与可用采集器")
async def workflows(actor: str = Depends(require_auth)) -> dict:
    settings = get_settings()
    items = load_workflows(settings)
    return {
        "enabled": settings.workflow_enabled,
        "path": str(settings.workflow_path),
        "budget_seconds": settings.workflow_timeout,
        "step_timeout_seconds": settings.workflow_step_timeout,
        "providers": list_providers(),
        "items": [
            {
                "id": w.id,
                "name": w.name,
                "description": w.description,
                "enabled": w.enabled,
                "triggers": [
                    {
                        "rules": t.rules,
                        "severity": t.severity,
                        "source": t.source,
                        "target_regex": t.target_regex,
                    }
                    for t in w.triggers
                    if not t.is_empty()
                ],
                "steps": [
                    {"name": s.name, "provider": s.provider} for s in w.steps
                ],
            }
            for w in items
        ],
    }
