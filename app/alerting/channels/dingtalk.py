"""钉钉机器人渠道。"""

from __future__ import annotations

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
