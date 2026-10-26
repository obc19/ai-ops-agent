"""飞书机器人渠道。"""

from __future__ import annotations

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
