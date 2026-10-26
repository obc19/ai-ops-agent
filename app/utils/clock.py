"""时间工具：存储统一 UTC，展示统一 Asia/Shanghai。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config.constants import DISPLAY_TZ

try:  # 容器精简镜像与 Windows 都可能缺 tzdata，降级为固定 +08:00
    LOCAL_TZ: timezone | ZoneInfo = ZoneInfo(DISPLAY_TZ)
except Exception:  # noqa: BLE001 - 缺少时区数据库属预期情况
    LOCAL_TZ = timezone(timedelta(hours=8), DISPLAY_TZ)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ)


def fmt_local(value: datetime | float) -> str:
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    return to_local(value).strftime("%Y-%m-%d %H:%M:%S")


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def window_before(minutes: int) -> tuple[datetime, datetime]:
    end = utc_now()
    return end - timedelta(minutes=minutes), end


def humanize_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes} 分 {sec} 秒"
