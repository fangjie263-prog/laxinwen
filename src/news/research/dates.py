"""日期识别（**文件名 > metadata > mtime > Unknown**）与格式化。

需求（第三阶段，固定顺序）::

    1. filename   文件名里的明确日期（20260912 / 2026-09-12 / 2026_09_12）
    2. metadata   内嵌元数据（PDF / DOCX / EXIF；由调用方读取后传入）
    3. modified   文件修改时间（st_mtime）
    4. Unknown    全部失败 —— 不再回退到今天，避免产生假日期

**``ctime``（``st_ctime``）不是正常日期来源**：POSIX 上它是 inode 变更时间，
与文档时间无关。这里彻底不再把它当作日期来源。

目录统一 ``YYYY-MM-DD``；文件名前缀统一 ``YYYYMMDD``。
日志必须能显示 ``date=2026-09-12(source=filename)`` 形式。
"""

from __future__ import annotations

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
    """日期识别结果。

    ``matched_by`` 取值：``filename`` / ``metadata`` / ``modified`` / ``unknown``。
    """

    date: date
    matched_by: str  # "filename" / "metadata" / "modified" / "unknown"

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


def detect_date_from_metadata(metadata: dict | None) -> Optional[date]:
    """从内嵌元数据里取日期（``metadata`` 由调用方读取）。

    支持的键（按可信度）：``date`` / ``creation_date`` / ``created`` /
    ``modified`` / ``modification_date``。值可以是 ``date`` / ``datetime``
    / ``YYYY-MM-DD`` / ``YYYYMMDD`` 字符串。
    """
    if not metadata:
        return None
    keys = ("date", "creation_date", "created", "modified", "modification_date", "mtime")
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        parsed = detect_date_from_filename(str(value))
        if parsed is not None:
            return parsed
    return None


def detect_date(
    path: str | Path,
    *,
    filename: str | None = None,
    metadata: dict | None = None,
) -> DateMatch:
    """按固定顺序识别日期：``filename → metadata → mtime → Unknown``。

    ``ctime`` 明确**不作为**日期来源。
    """
    file_path = Path(path)
    text = filename if filename is not None else file_path.name

    # 1) 文件名
    from_name = detect_date_from_filename(text)
    if from_name is not None:
        return DateMatch(from_name, "filename")

    # 2) 内嵌元数据（PDF / DOCX / EXIF）
    from_metadata = detect_date_from_metadata(metadata)
    if from_metadata is not None:
        return DateMatch(from_metadata, "metadata")

    # 3) 修改时间（st_mtime）
    try:
        stat = file_path.stat()
    except OSError:
        return DateMatch(date.today(), "unknown")

    modified = _from_timestamp(stat.st_mtime)
    if modified is not None:
        return DateMatch(modified, "modified")

    # 4) Unknown（不再用 ctime 兜底，也不假造「今天」）
    return DateMatch(date.today(), "unknown")


def today() -> date:
    return date.today()


def timestamp_now() -> float:
    return time.time()
