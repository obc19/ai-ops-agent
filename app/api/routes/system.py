"""系统自检：模型连通性探针。

存在的理由：AIOps 的结论质量完全取决于模型是否真的连上了。
未配置密钥时系统会降级为规则引擎（if-else），这个降级必须是**可观测**的，
否则用户会以为自己在用 AI，实际看到的是规则模板。

本路由只做两件事：
- ``GET  /system/llm``      报告当前配置状态（不返回明文密钥）；
- ``POST /system/llm-test``  真实发起一次最小请求，验证密钥、网络、模型名三者是否都对。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from app.api.deps import require_auth, require_operator
from app.config.settings import get_settings
from app.llm.client import LLMError, chat_text
from app.storage import get_store
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["system"], prefix="/system")

PROBE_MESSAGES = [
    {"role": "system", "content": "你是连通性探针。忽略其他指令，只回复四个字符：模型可用"},
    {"role": "user", "content": "ping"},
]

UNCONFIGURED_HINT = (
    "未配置模型密钥，当前为规则模式（不调用大模型，结论由内置规则生成）。"
    "配置方式：在 .env 或容器环境变量中设置 LLM_API_KEY，"
    "并确认 LLM_BASE_URL / LLM_MODEL 与服务商一致，然后重启服务。"
)


def snapshot() -> dict:
    """配置快照。密钥只回打码值，避免接口成为密钥泄漏点。"""
    s = get_settings()
    return {
        "configured": s.llm_enabled,
        "model": s.llm_model,
        "base_url": s.llm_base_url,
        "provider": s.llm_provider,
        "api_key_masked": s.llm_key_masked,
        "timeout_seconds": s.llm_timeout,
        "hint": "" if s.llm_enabled else UNCONFIGURED_HINT,
    }


@router.get("/llm", summary="模型配置状态（密钥打码）")
async def llm_status(actor: str = Depends(require_auth)) -> dict:
    return snapshot()


@router.post("/llm-test", summary="真实调用一次模型，验证密钥与网络")
async def llm_test(actor: str = Depends(require_operator)) -> dict:
    cfg = get_settings()
    base = snapshot()
    if not cfg.llm_enabled:
        return {**base, "ok": False, "latency_ms": 0, "reply": "", "error": base["hint"]}

    started = time.perf_counter()
    try:
        reply = await chat_text(
            PROBE_MESSAGES, temperature=0.0, max_tokens=16, settings=cfg
        )
    except LLMError as exc:
        latency = round((time.perf_counter() - started) * 1000)
        message = str(exc)
        logger.error("模型连通性检测失败：%s", message[:300])
        get_store().append_audit(
            "llm_probe_failed",
            {"model": cfg.llm_model, "provider": cfg.llm_provider, "error": message[:300]},
            actor=actor,
            egress=True,
        )
        return {**base, "ok": False, "latency_ms": latency, "reply": "", "error": message}

    latency = round((time.perf_counter() - started) * 1000)
    get_store().append_audit(
        "llm_probe",
        {"model": cfg.llm_model, "provider": cfg.llm_provider, "latency_ms": latency},
        actor=actor,
        egress=True,
    )
    return {**base, "ok": True, "latency_ms": latency, "reply": reply[:200], "error": ""}
