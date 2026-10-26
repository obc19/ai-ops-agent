"""钉钉机器人渠道（支持「加签」校验）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

from app.alerting.channels.base import BaseChannel
from app.models.alert import Alert


class DingTalkChannel(BaseChannel):
    name = "dingtalk"

    @property
    def configured_url(self) -> str:
        return self.settings.dingtalk_webhook

    def build_payload(self, alert: Alert) -> dict:
        steps = "\n".join(f"- {s}" for s in alert.suggestion[:5])
        text = (
            f"### {alert.severity} 故障告警\n\n"
            f"- **对象**：{alert.cluster}/{alert.namespace}/{alert.target}\n"
            f"- **指标**：{alert.metric}\n"
            f"- **根因**：{alert.root_cause or '待诊断'}\n"
            f"- **摘要**：{alert.summary}\n"
        )
        if steps:
            text += f"\n**处置建议**\n{steps}\n"
        return {
            "msgtype": "markdown",
            "markdown": {"title": f"{alert.severity} 故障告警", "text": text},
        }

    def build_request(self, alert: Alert) -> tuple[str, dict]:
        url, payload = self.endpoint, self.build_payload(alert)
        secret = self.settings.dingtalk_secret
        if secret and self.configured_url:  # mock 接收端不需要加签
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            digest = hmac.new(
                secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
            ).digest()
            sign = quote_plus(base64.b64encode(digest))
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}timestamp={timestamp}&sign={sign}"
        return url, payload
