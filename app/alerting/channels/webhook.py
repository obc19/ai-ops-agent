"""通用 Webhook 渠道：POST 完整告警 JSON，便于对接自有系统。"""

from __future__ import annotations

from app.alerting.channels.base import BaseChannel
from app.models.alert import Alert


class WebhookChannel(BaseChannel):
    name = "webhook"

    @property
    def configured_url(self) -> str:
        return self.settings.alert_webhook_url

    def build_payload(self, alert: Alert) -> dict:
        return {
            "source": "ai-ops-agent",
            "event": "alert.triggered",
            "alert": alert.model_dump(mode="json"),
        }
