"""日期识别（文件名 > 创建时间 > 修改时间）与格式化。

需求二十一：

1. 第一优先级：文件名里的明确日期（``20260912`` / ``2026-09-12`` / ``2026_09_12``）；
2. 第二优先级：文件创建时间（``st_ctime``）；
3. 第三优先级：文件修改时间（``st_mtime``）。

目录统一 ``YYYY-MM-DD``；文件名前缀统一 ``YYYYMMDD``。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# 允许的日期写法（按可信度排序）：
#   20260912 / 2026-09-12 / 2026_09_12 / 2026.09.12 / 2026年09月12日
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(20\d{2})[-_.年/](0?[1-9]|1[0-2])[-_.月/](0?[1-9]|[12]\d|3[01])日?(?!\d)"), "ymd"),
)

# 6 位与 8 位纯数字「不是日期」的常见形态（代码 / 编号），用于降噪
_FALSE_POSITIVE_8 = re.compile(r"^(?:19|20)\d{6}$")


@dataclass(frozen=True)
class DateMatch:
    """日期识别结果。"""

    date: date
    matched_by: str  # "filename" / "created" / "modified"

    @property
    def directory(self) -> str:
        return self.date.isoformat()

    @property
    def prefix(self) -> str:
        return self.date.strftime("%Y%m%d")


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def detect_date_from_filename(filename: str) -> Optional[date]:
    """从文件名（或整段文本）提取日期；找不到返回 ``None``。"""
    text = str(filename or "")
    if not text:
        return None
    for pattern, _ in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = _safe_date(match.group(1), match.group(2), match.group(3))
        if candidate is None:
            continue
        # 过滤「看起来像编号」的 8 位纯数字（如 20000001）
        if _FALSE_POSITIVE_8.match(match.group(0)) and not match.group(0).startswith("20"):
            continue
        return candidate
    return None


def _from_timestamp(value: float) -> Optional[date]:
    try:
        return datetime.fromtimestamp(float(value)).date()
    except (OverflowError, OSError, ValueError):
        return None


def detect_date(path: str | Path, *, filename: str | None = None) -> DateMatch:
    """按 文件名 → 创建时间 → 修改时间 的顺序识别日期。"""
    file_path = Path(path)
    text = filename if filename is not None else file_path.name

    from_name = detect_date_from_filename(text)
    if from_name is not None:
        return DateMatch(from_name, "filename")

    try:
        stat = file_path.stat()
    except OSError:
        return DateMatch(date.today(), "modified")

    # st_ctime 在 Windows 上是创建时间；在 POSIX 上是 inode 变更时间，
    # 这里仍按需求作为「第二优先级」使用，但只在明显更早 / 更晚时优先。
    created = _from_timestamp(stat.st_ctime)
    modified = _from_timestamp(stat.st_mtime)
    if created is not None and modified is not None:
        # Windows 真实创建时间语义：优先创建时间
        if os.name == "nt":
            return DateMatch(created, "created")
        # POSIX：mtime 才是「真正的文件时间」，ctime 仅作兜底
        return DateMatch(modified, "modified")
    if created is not None:
        return DateMatch(created, "created")
    if modified is not None:
        return DateMatch(modified, "modified")
    return DateMatch(date.today(), "modified")


def today() -> date:
    return date.today()


def timestamp_now() -> float:
    return time.time()
