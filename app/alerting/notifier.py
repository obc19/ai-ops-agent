"""告警分发：并发投递多渠道，任一成功即视为送达。"""

from __future__ import annotations

import asyncio

from app.alerting.channels import build_channels
from app.config.settings import Settings, get_settings
from app.models.alert import Alert, NotificationResult
from app.utils.logging import get_logger

logger = get_logger(__name__)


class Notifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        names = [n for n in self.settings.alert_channels.split(",") if n.strip()]
        self.channels = build_channels(names, self.settings)

    async def dispatch(self, alert: Alert) -> list[NotificationResult]:
        if not self.channels:
            logger.warning("未配置任何告警渠道")
            return []
        results = await asyncio.gather(
            *(channel.send(alert) for channel in self.channels), return_exceptions=True
        )
        final: list[NotificationResult] = []
        for channel, result in zip(self.channels, results):
            if isinstance(result, NotificationResult):
                final.append(result)
            else:
                logger.error("%s 渠道异常：%s", channel.name, result)
                final.append(
                    NotificationResult(channel=channel.name, ok=False, detail=str(result)[:150])
                )
        succeeded = [r.channel for r in final if r.ok]
        logger.info(
            "告警 %s 投递完成：成功 %s / 共 %d",
            alert.id,
            succeeded or "无",
            len(final),
        )
        return final
