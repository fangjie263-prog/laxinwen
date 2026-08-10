"""北京时间（Asia/Shanghai）展示辅助。

需求：所有新闻展示时间统一使用北京时间（Asia/Shanghai），24 小时制。

内部存储与导出仍统一使用 UTC（ISO 8601），仅在「展示层」把 UTC 时间
转换为 Asia/Shanghai 并格式化为 24 小时制字符串。数据源不变。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

# 北京时区（Asia/Shanghai，无夏令时，固定 UTC+8）
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 展示后缀，标识时间为北京时间
BEIJING_LABEL = "北京时间"


def to_beijing(dt: Optional[datetime]) -> Optional[datetime]:
    """把任意带时区/不带时区的 datetime 转换为北京时间。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ)


def now_beijing() -> datetime:
    """返回当前北京时间（带时区）。"""
    return datetime.now(BEIJING_TZ)


def fmt_dt(value) -> str:
    """把 ISO 时间字符串格式化为北京时间 ``YYYY-MM-DD HH:MM``（24 小时制）；空值返回 '—'。"""
    dt = _parse(value)
    if dt is None:
        return "—"
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def fmt_date(value) -> str:
    """把 ISO 时间字符串格式化为北京时间 ``YYYY-MM-DD``；空值返回 '—'。"""
    dt = _parse(value)
    if dt is None:
        return "—"
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
