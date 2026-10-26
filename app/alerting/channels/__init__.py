"""通知渠道注册表：新增渠道只需实现 BaseChannel 并在此登记。"""

from __future__ import annotations

from app.alerting.channels.base import BaseChannel
from app.alerting.channels.dingtalk import DingTalkChannel
from app.alerting.channels.email import EmailChannel
from app.alerting.channels.feishu import FeishuChannel
from app.alerting.channels.webhook import WebhookChannel

CHANNEL_TYPES: dict[str, type[BaseChannel]] = {
    "feishu": FeishuChannel,
    "dingtalk": DingTalkChannel,
    "email": EmailChannel,
    "webhook": WebhookChannel,
}


def build_channels(names: list[str], settings=None) -> list[BaseChannel]:
    channels = []
    for name in names:
        cls = CHANNEL_TYPES.get(name.strip().lower())
        if cls is None:
            continue
        channels.append(cls(settings) if settings is not None else cls())
    return channels


__all__ = [
    "BaseChannel",
    "CHANNEL_TYPES",
    "build_channels",
    "FeishuChannel",
    "DingTalkChannel",
    "EmailChannel",
    "WebhookChannel",
]
