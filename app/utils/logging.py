"""统一日志：UTC 记录、当地时区展示、Windows 控制台强制 UTF-8。"""

from __future__ import annotations

import logging
import sys

from app.config.settings import get_settings
from app.utils.clock import fmt_local

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


class LocalTimeFormatter(logging.Formatter):
    """把 UTC 时间渲染成 Asia/Shanghai 便于运维阅读。"""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - 覆写标准方法
        return fmt_local(record.created)


def setup_logging(level: str | None = None) -> None:
    settings = get_settings()
    level = (level or settings.log_level).upper()

    # Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # 流已被接管时忽略
                pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        LocalTimeFormatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s")
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # 降低第三方噪音
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
