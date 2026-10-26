"""邮件渠道：SMTP 发送；mock 模式下投递到内置接收端便于验收。"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from time import perf_counter

import httpx

from app.alerting.channels.base import BaseChannel
from app.models.alert import Alert, NotificationResult
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EmailChannel(BaseChannel):
    name = "email"

    @property
    def configured_url(self) -> str:
        return ""

    def build_payload(self, alert: Alert) -> dict:
        body = self.summary_line(alert)
        if alert.suggestion:
            body += "\n\n处置建议：\n" + "\n".join(
                f"{i}. {s}" for i, s in enumerate(alert.suggestion[:5], 1)
            )
        return {
            "subject": f"[{alert.severity}] {alert.cluster}/{alert.target} {alert.metric}",
            "to": self.settings.alert_email_to,
            "body": body,
            "alert_id": alert.id,
        }

    async def send(self, alert: Alert) -> NotificationResult:
        started = perf_counter()
        payload = self.build_payload(alert)

        if self.settings.smtp_host and self.settings.alert_email_to:
            ok, detail = await self._send_smtp(payload)
        elif self.settings.mock_mode:
            ok, detail = await self._post_sink(payload)
        else:
            ok, detail = False, "未配置 SMTP，已跳过"

        return NotificationResult(
            channel=self.name,
            ok=ok,
            detail=detail,
            latency_ms=round((perf_counter() - started) * 1000, 1),
        )

    async def _post_sink(self, payload: dict) -> tuple[bool, str]:
        url = f"{self.settings.sink_base.rstrip('/')}/{self.name}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(url, json=payload)
            return resp.status_code < 400, f"mock 投递 HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            return False, f"mock 投递失败：{type(exc).__name__}"

    async def _send_smtp(self, payload: dict) -> tuple[bool, str]:
        def _blocking() -> tuple[bool, str]:
            message = EmailMessage()
            message["Subject"] = payload["subject"]
            message["From"] = self.settings.alert_email_from or self.settings.smtp_user
            message["To"] = payload["to"]
            message.set_content(payload["body"])
            try:
                if self.settings.smtp_use_tls:
                    server = smtplib.SMTP_SSL(
                        self.settings.smtp_host, self.settings.smtp_port, timeout=self.timeout
                    )
                else:
                    server = smtplib.SMTP(
                        self.settings.smtp_host, self.settings.smtp_port, timeout=self.timeout
                    )
                with server:
                    if self.settings.smtp_user:
                        server.login(self.settings.smtp_user, self.settings.smtp_password)
                    server.send_message(message)
                return True, "SMTP 已投递"
            except (smtplib.SMTPException, OSError) as exc:
                logger.warning("邮件发送失败：%s", exc)
                return False, f"SMTP 失败：{type(exc).__name__}"

        return await asyncio.to_thread(_blocking)
