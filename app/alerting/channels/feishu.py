"""飞书机器人渠道（支持「加签」校验）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app.alerting.channels.base import BaseChannel
from app.models.alert import Alert


class FeishuChannel(BaseChannel):
    name = "feishu"

    @property
    def configured_url(self) -> str:
        return self.settings.feishu_webhook

    def build_payload(self, alert: Alert) -> dict:
        suggestions = "\n".join(f"{i}. {s}" for i, s in enumerate(alert.suggestion[:5], 1))
        text = self.summary_line(alert)
        if suggestions:
            text += f"\n处置建议：\n{suggestions}"
        return {
            "msg_type": "text",
            "content": {"text": f"[ai-ops-agent] {text}"},
        }

    def build_request(self, alert: Alert) -> tuple[str, dict]:
        url, payload = self.endpoint, self.build_payload(alert)
        secret = self.settings.feishu_secret
        if secret and self.configured_url:  # mock 接收端不需要加签
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{secret}"
            digest = hmac.new(
                string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
            ).digest()
            payload = {
                **payload,
                "timestamp": timestamp,
                "sign": base64.b64encode(digest).decode("utf-8"),
            }
        return url, payload
