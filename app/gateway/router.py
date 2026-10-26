"""数据安全网关：外发前的唯一关口。

分流规则（与确认书 D4/D15 一致）：
- 敏感数据 + 本地模型可用 → 走本地通道，原文不出境；
- 敏感数据 + 本地模型不可用 → 脱敏后再走外部通道（脱敏后内容才外发）；
- 非敏感数据 → 直接走外部通道；
- 脱敏引擎异常 / 脱敏后仍检出敏感 → 一律拦截（fail-closed），写审计并告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.config.settings import Settings, get_settings
from app.gateway.desensitizer import (
    Desensitizer,
    DesensitizerError,
    Finding,
    get_desensitizer,
)
from app.llm.client import LLMError, LLMUnavailable, chat_text
from app.utils.logging import get_logger

logger = get_logger(__name__)

Channel = Literal["local", "external", "external-redacted", "blocked"]


class EgressBlocked(RuntimeError):
    """脱敏失败或二次校验未通过，外发被拦截。"""


@dataclass
class EgressResult:
    text: str = ""
    channel: Channel = "external"
    ok: bool = False
    redacted_preview: str = ""
    findings: list[Finding] = field(default_factory=list)
    error: str = ""
    model: str = ""

    @property
    def used_llm(self) -> bool:
        return self.ok and self.channel != "blocked"

    @property
    def sensitive(self) -> bool:
        return bool(self.findings)


class SafetyGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        desensitizer: Desensitizer | None = None,
        store=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.desensitizer = desensitizer or get_desensitizer()
        self._store = store

    @property
    def store(self):
        if self._store is None:
            from app.storage import get_store

            self._store = get_store()
        return self._store

    # ---------- 外发主流程 ----------
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "diagnosis",
        actor: str = "system",
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> EgressResult:
        joined = "\n".join(m.get("content", "") for m in messages)

        try:
            result = self.desensitizer.redact(joined)
        except DesensitizerError as exc:
            self._fail_closed(purpose, actor, "desensitizer_error", str(exc))
            raise EgressBlocked(str(exc)) from exc

        # 二次校验：脱敏后仍命中规则 → 宁可拦截也不外发
        try:
            residual = self.desensitizer.scan(result.text)
        except DesensitizerError as exc:
            self._fail_closed(purpose, actor, "residual_scan_error", str(exc))
            raise EgressBlocked(str(exc)) from exc
        if residual:
            detail = "；".join(f"{item.rule_name}×{item.count}" for item in residual[:5])
            self._fail_closed(purpose, actor, "residual_sensitive", detail)
            raise EgressBlocked(f"脱敏后仍检出敏感内容：{detail}")

        # 本地模型通道：敏感数据不出境
        if result.findings and self.settings.local_model_enabled:
            return await self._call_local(messages, purpose, actor, result.findings)

        if result.findings and not self.settings.local_model_enabled:
            logger.warning(
                "本地模型未启用，敏感数据将脱敏后外发（%s）", result.summary
            )

        if not self.settings.llm_enabled:
            raise LLMUnavailable("未配置外部 LLM，且本地模型不可用")

        # 用脱敏后的内容替换原消息文本
        safe_messages = self._rewrite(messages, result.text, joined)
        try:
            text = await chat_text(
                safe_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                settings=self.settings,
            )
        except LLMError as exc:
            self.store.append_audit(
                "llm_egress_failed",
                {"purpose": purpose, "error": str(exc)[:300]},
                actor=actor,
                egress=True,
            )
            raise

        channel: Channel = "external-redacted" if result.findings else "external"
        self.store.append_audit(
            "llm_egress",
            {
                "purpose": purpose,
                "channel": channel,
                "model": self.settings.llm_model,
                "findings": [f.to_dict() for f in result.findings],
                "preview": result.text[:300],
            },
            actor=actor,
            target=purpose,
            egress=True,
        )
        return EgressResult(
            text=text,
            channel=channel,
            ok=True,
            redacted_preview=result.text[:300],
            findings=result.findings,
            model=self.settings.llm_model,
        )

    async def _call_local(
        self, messages, purpose: str, actor: str, findings: list[Finding]
    ) -> EgressResult:
        try:
            text = await chat_text(
                messages,
                model=self.settings.local_model_name,
                settings=self.settings.model_copy(
                    update={
                        "llm_base_url": self.settings.local_model_url,
                        "llm_api_key": "local",
                        "llm_model": self.settings.local_model_name,
                    }
                ),
            )
        except LLMError as exc:
            logger.warning("本地模型调用失败，降级为脱敏后外发：%s", exc)
            self.store.append_audit(
                "local_model_failed", {"error": str(exc)[:200]}, actor=actor, egress=False
            )
            raise

        self.store.append_audit(
            "local_llm_call",
            {"purpose": purpose, "model": self.settings.local_model_name,
             "findings": [f.to_dict() for f in findings]},
            actor=actor,
            target=purpose,
            egress=False,
        )
        return EgressResult(
            text=text,
            channel="local",
            ok=True,
            findings=findings,
            model=self.settings.local_model_name,
        )

    @staticmethod
    def _rewrite(
        messages: list[dict[str, str]], safe_joined: str, original_joined: str
    ) -> list[dict[str, str]]:
        """把脱敏后的整体文本尽量按原段落还原到消息里。"""
        if safe_joined == original_joined:
            return messages
        safe_parts = safe_joined.split("\n")
        idx = 0
        output: list[dict[str, str]] = []
        for message in messages:
            length = message.get("content", "").count("\n") + 1
            chunk = "\n".join(safe_parts[idx : idx + length])
            idx += length
            output.append({**message, "content": chunk})
        return output

    def _fail_closed(self, purpose: str, actor: str, metric: str, detail: str) -> None:
        """fail-closed：写审计 + 触发工具自身 P1 告警（不依赖 LLM，避免递归）。"""
        self.store.append_audit(
            "llm_egress_blocked",
            {"purpose": purpose, "metric": metric, "reason": detail[:300]},
            actor=actor,
            target=purpose,
            egress=True,
        )
        logger.error("外发已拦截（%s）：%s", metric, detail[:200])
        try:
            from app.alerting.policy import raise_system_alert

            raise_system_alert(
                metric,
                summary=f"外发被拦截：{metric}",
                detail=f"用途 {purpose}｜{detail[:200]}",
                store=self.store,
            )
        except Exception as exc:  # noqa: BLE001 - 自监控失败不得掩盖原始拦截
            logger.warning("记录自监控告警失败：%s", exc)

    # ---------- 供外部检查 ----------
    def inspect(self, text: str):
        try:
            return self.desensitizer.redact(text)
        except DesensitizerError as exc:
            raise EgressBlocked(str(exc)) from exc


_gateway: SafetyGateway | None = None


def get_gateway() -> SafetyGateway:
    global _gateway
    if _gateway is None:
        _gateway = SafetyGateway()
    return _gateway
