"""统一的导出运行身份。

run_id 表示一次实际运行，job_id 只表示任务配置。所有 HTML/Word artifact
必须复用同一个 RunIdentity；本模块不参与抓取或数据库逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


_RUN_RE = re.compile(r"^(?P<date>\d{8})-(?P<time>\d{6})(?P<suffix>-\d{2})?$")


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    started_at: datetime
    job_id: str = ""

    @property
    def display_time(self) -> str:
        return self.started_at.strftime("%H:%M:%S")

    @property
    def display_label(self) -> str:
        return f"{self.display_time} · {self.job_id or '手动运行'}"


def portable_package_name(
    source_id: str, started_at: datetime, run_id: str, job_id: str = ""
) -> str:
    """返回统一的 Portable 包目录名。

    日期来自运行开始时间，目录只保留 run_id 的时间部分，避免重复写入日期；
    ``run_id`` 本身仍保持 ``YYYYMMDD-HHMMSS[-序号]`` 格式。
    """
    run_time = run_id.split("-", 1)[1][:6] if "-" in run_id else started_at.strftime("%H%M%S")
    job_suffix = f"-{job_id}" if job_id else ""
    return (
        f"Laxinwen-{source_id.upper()}-{started_at:%Y-%m-%d}-"
        f"{run_time}{job_suffix}"
    )


def new_run_identity(
    *,
    job_id: str = "",
    started_at: Optional[datetime] = None,
    output_root: str | Path | None = None,
    source_id: str = "",
) -> RunIdentity:
    """在一次运行开始时创建 run_id；同秒已有目录时追加序号。"""
    started_at = started_at or datetime.now().astimezone().replace(tzinfo=None)
    base = started_at.strftime("%Y%m%d-%H%M%S")
    run_id = base
    if output_root and source_id:
        root = Path(output_root)
        source = source_id.upper()
        date = started_at.strftime("%Y-%m-%d")
        suffix = f"-{job_id}" if job_id else ""
        index = 2
        new_name = portable_package_name(source, started_at, run_id, job_id)
        old_name = f"Laxinwen-{source}-{date}-{run_id}{suffix}"
        while (root / new_name).exists() or (root / old_name).exists():
            run_id = f"{base}-{index:02d}"
            new_name = portable_package_name(source, started_at, run_id, job_id)
            old_name = f"Laxinwen-{source}-{date}-{run_id}{suffix}"
            index += 1
    return RunIdentity(run_id=run_id, started_at=started_at, job_id=job_id)


def parse_run_id(value: str) -> Optional[datetime]:
    """解析新 run_id；旧目录没有 run_id 时返回 None。"""
    match = _RUN_RE.match(value)
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
