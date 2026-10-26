"""通知渠道基类：统一的发送、计时与错误处理。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from time import perf_counter

import httpx

from app.config.settings import Settings, get_settings
from app.models.alert import Alert, NotificationResult
from app.utils.logging import get_logger

logger = get_logger(__name__)


class BaseChannel(ABC):
    name: str = "base"
    timeout: float = 8.0

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------- 子类实现 ----------
    @property
    @abstractmethod
    def configured_url(self) -> str:
        """真实渠道地址；为空时 mock 模式回退到内置接收端。"""

    @abstractmethod
    def build_payload(self, alert: Alert) -> dict:
        """平台特定的消息体。"""

    # ---------- 通用 ----------
    @property
    def endpoint(self) -> str:
        url = self.configured_url
        if url:
            return url
        if self.settings.mock_mode:
            return f"{self.settings.mock_sink_base.rstrip('/')}/{self.name}"
        return ""

    async def send(self, alert: Alert) -> NotificationResult:
        started = perf_counter()
        url = self.endpoint
        if not url:
            return NotificationResult(
                channel=self.name, ok=False, detail="未配置渠道地址，已跳过"
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(url, json=self.build_payload(alert))
            ok = resp.status_code < 400
            detail = f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code}: {resp.text[:120]}"
        except httpx.HTTPError as exc:
            ok = False
            detail = f"发送失败：{type(exc).__name__}"
        latency = round((perf_counter() - started) * 1000, 1)
        if not ok:
            logger.warning("%s 渠道发送失败：%s", self.name, detail)
        return NotificationResult(
            channel=self.name, ok=ok, detail=detail, latency_ms=latency
        )

    @staticmethod
    def summary_line(alert: Alert) -> str:
        from app.utils.clock import fmt_local

        return (
            f"【{alert.severity}】{alert.summary}\n"
            f"对象：{alert.cluster}/{alert.namespace}/{alert.target}\n"
            f"指标：{alert.metric}｜时间：{fmt_local(alert.window.start)} ~ "
            f"{fmt_local(alert.window.end)}\n"
            f"根因：{alert.root_cause or '待诊断'}"
        )
