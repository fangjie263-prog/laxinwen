"""AI Research Archive 配置（复用 Laxinwen 现有配置方式）。

现有项目**没有**统一的应用配置模块（``config.py`` 只负责 ``sites/*.yaml``），
全局配置走 ``.env`` + 代码内默认路径。因此本功能沿用同一种方式：

- 值来自环境变量 / ``.env``（与 ``NOTION_TOKEN`` / ``NOTION_ROOT_PAGE_ID`` 一致）；
- 不新增配置文件格式，不改动 ``scheduler_config.py`` 的既有字段语义；
- 所有路径默认落在项目 ``data/`` 下，Windows Task Scheduler 从任意 cwd 启动都可靠。

环境变量一览::

    RESEARCH_INBOX_DIR       研究投递目录（默认 <项目>/data/research/inbox）
    RESEARCH_ARCHIVE_DIR     归档目录（默认 <项目>/data/research/archive）
    RESEARCH_FAILED_DIR      异常目录（默认 <项目>/data/research/failed）
    RESEARCH_DB              状态/去重库（默认 <项目>/data/research/research_archive.db）
    RESEARCH_COMPANIES_FILE  公司映射表（默认 <项目>/data/research/companies.json）
    RESEARCH_DRY_RUN         默认是否 dry-run（默认 true；必须显式关闭才会移动文件）
    RESEARCH_MAX_UPLOAD_MB   单文件上限（默认 4.5，与 Notion 一致）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .sanitize import MAX_ARCHIVE_FILE_SIZE

# 项目根：src/news/research/config.py → 向上三级
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATA = _PROJECT_ROOT / "data" / "research"

# 处理范围（需求三）：只处理最终研究成果。
# ``.doc``（旧版 Word 二进制格式）必须一起支持：真实投递目录里确实存在
# ``SK海力士20260831.doc`` 这类文件，不能被静默忽略。
SUPPORTED_EXTENSIONS = (
    ".pdf", ".docx", ".doc", ".html", ".htm",
    ".md", ".txt", ".xls", ".xlsx",
)

# 明确忽略（不处理）的类型，写出来是为了让行为可读、可测
IGNORED_EXTENSIONS = (
    ".markdown", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".bmp", ".tif", ".tiff", ".heic", ".tmp", ".part", ".crdownload",
    ".zip", ".rar", ".7z", ".json", ".csv", ".pptx", ".ppt",
)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class ResearchArchiveConfig:
    """一次归档运行所需的全部配置。"""

    inbox_dir: Path = _DEFAULT_DATA / "inbox"
    archive_dir: Path = _DEFAULT_DATA / "archive"
    failed_dir: Path = _DEFAULT_DATA / "failed"
    db_path: Path = _DEFAULT_DATA / "research_archive.db"
    companies_file: Optional[Path] = _DEFAULT_DATA / "companies.json"
    dry_run: bool = True
    max_upload_bytes: int = MAX_ARCHIVE_FILE_SIZE
    max_files: int = 0  # 0 = 不限制
    recursive: bool = False

    # ---------- 派生属性 ----------

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return SUPPORTED_EXTENSIONS

    def is_supported(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

    def ensure_dirs(self) -> None:
        """创建所需目录（dry-run 下也创建，便于用户看到结构）。"""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict:
        return {
            "inbox_dir": str(self.inbox_dir),
            "archive_dir": str(self.archive_dir),
            "failed_dir": str(self.failed_dir),
            "db_path": str(self.db_path),
            "companies_file": str(self.companies_file) if self.companies_file else "",
            "dry_run": self.dry_run,
            "max_upload_bytes": self.max_upload_bytes,
            "max_files": self.max_files,
            "recursive": self.recursive,
            "supported_extensions": list(SUPPORTED_EXTENSIONS),
        }


def load_research_config(
    *,
    inbox_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
    failed_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    companies_file: str | Path | None = None,
    dry_run: bool | None = None,
    max_files: int | None = None,
    recursive: bool | None = None,
) -> ResearchArchiveConfig:
    """加载配置：**显式参数 > 环境变量 / .env > 默认值**。

    显式 ``dry_run=False`` 才会真正移动文件；未显式指定时由
    ``RESEARCH_DRY_RUN`` 决定，默认 ``True``（安全优先）。
    """
    from ..ai.provider import load_dotenv

    load_dotenv()

    max_upload_bytes = int(
        _env_float("RESEARCH_MAX_UPLOAD_MB", MAX_ARCHIVE_FILE_SIZE / (1024 * 1024))
        * 1024
        * 1024
    )
    resolved_companies = (
        Path(companies_file).expanduser()
        if companies_file
        else _env_path("RESEARCH_COMPANIES_FILE", _DEFAULT_DATA / "companies.json")
    )
    return ResearchArchiveConfig(
        inbox_dir=Path(inbox_dir).expanduser()
        if inbox_dir
        else _env_path("RESEARCH_INBOX_DIR", _DEFAULT_DATA / "inbox"),
        archive_dir=Path(archive_dir).expanduser()
        if archive_dir
        else _env_path("RESEARCH_ARCHIVE_DIR", _DEFAULT_DATA / "archive"),
        failed_dir=Path(failed_dir).expanduser()
        if failed_dir
        else _env_path("RESEARCH_FAILED_DIR", _DEFAULT_DATA / "failed"),
        db_path=Path(db_path).expanduser()
        if db_path
        else _env_path("RESEARCH_DB", _DEFAULT_DATA / "research_archive.db"),
        companies_file=resolved_companies,
        dry_run=_env_flag("RESEARCH_DRY_RUN", True) if dry_run is None else bool(dry_run),
        max_upload_bytes=max_upload_bytes,
        max_files=int(max_files) if max_files is not None else 0,
        recursive=_env_flag("RESEARCH_RECURSIVE", False) if recursive is None else bool(recursive),
    )
