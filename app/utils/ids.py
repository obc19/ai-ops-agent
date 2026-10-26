"""ID 生成：带前缀的短 ID，便于日志检索与人工阅读。"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


def short_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2 + 1)[:length]
