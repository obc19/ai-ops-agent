"""轮询检测调度器：周期采集 → 检测 → 诊断 → 告警，并定期清理过期数据。"""

from __future__ import annotations

import asyncio

from app.config.settings import get_settings
from app.services.pipeline import get_pipeline
from app.storage import get_store
from app.utils.logging import get_logger

logger = get_logger(__name__)


class DetectionScheduler:
    def __init__(self, interval: int | None = None) -> None:
        self.settings = get_settings()
        self.interval = max(interval or self.settings.detect_interval, 1)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.rounds = 0
        self.last_error = ""

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="detection-scheduler")
        logger.info("检测调度已启动：周期 %ss", self.interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
            logger.info("检测调度已停止（共执行 %d 轮）", self.rounds)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        pipeline = get_pipeline()
        store = get_store()
        store.purge(self.settings.retention_days)
        while not self._stop.is_set():
            try:
                results = await pipeline.run_detection_once()
                self.rounds += 1
                if results:
                    logger.info("第 %d 轮检测处理了 %d 条异常", self.rounds, len(results))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 调度循环不得因单轮失败退出
                self.last_error = str(exc)[:200]
                logger.exception("检测轮次失败，%ss 后重试", self.interval)
            # 每 360 轮（约 1 小时 @10s）清理一次过期数据
            if self.rounds and self.rounds % 360 == 0:
                store.purge(self.settings.retention_days)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue


_scheduler: DetectionScheduler | None = None


def get_scheduler() -> DetectionScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DetectionScheduler()
    return _scheduler
