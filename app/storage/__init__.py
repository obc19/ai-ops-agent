"""存储包：进程内单例 Store。"""

from __future__ import annotations

from functools import lru_cache

from app.config.settings import get_settings
from app.storage.store import Store


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(get_settings().db_path)


def reset_store() -> None:
    get_store.cache_clear()


__all__ = ["Store", "get_store", "reset_store"]
