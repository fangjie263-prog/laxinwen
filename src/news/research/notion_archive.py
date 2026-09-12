"""把研究成果写入 Notion —— **复用现有 NotionClient / 认证 / state**。

硬约束：

- 不新建第二套 Token、第二套 Client、第二套 Scheduler；
  ``NotionClient`` 直接从 ``news.notion_sync`` 导入，Token / Root Page 走同一个
  ``load_notion_config()``（``NOTION_TOKEN`` / ``NOTION_ROOT_PAGE_ID``）；
- 每次上传前先查 **SHA-256**：已归档且已成功上传则**跳过**，不重复上传；
- 上传失败时记录 ``notion_page_id`` 为空 + ``FAILED``，不会无限重试（同一 sha256
  已有失败记录且未变化时默认跳过重试，除非 ``--retry-failed``）。

页面结构（研究报告专属一级目录）：::

    Root(Laxinwen News)
      └── 研究报告            ← 固定中文名，已存在则复用
          └── 2026-09-12      ← 日期页，只创建一个
              └── 09696.HK｜天齐锂业   ← 公司页，只创建一个
                  └── Claude｜天齐锂业 ← 每个文件一个独立页面
                      ├── 2026-09-12 · 09696.HK
                      ├── 📎 研究报告
                      └── [文件附件]

与新闻归档结构（``Root → 来源 → 日期``）**完全隔离**，研究报告不再直接挂在
Root 下。Notion 文件页面保持极简：只在正文展示「日期 · Ticker / 📎 研究报告 /
附件」，不再暴露 SHA256、文件大小、分片等技术元数据（这些仍保存在 SQLite 用于
去重、恢复、Debug）。
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .archive_store import ResearchFileRecord, ResearchArchiveStore, STATUS_UPLOADED
from .config import ResearchArchiveConfig
from .scanner import ResearchCandidate

logger = logging.getLogger(__name__)

# 研究报告一级目录固定名称（需求二）：必须使用中文，且已存在时复用。
RESEARCH_CATEGORY_TITLE = "研究报告"

# 公司页面标题分隔符（需求四）：``Ticker｜Company``。
COMPANY_TITLE_SEPARATOR = "｜"

# 页面正文里不允许出现的字段名（需求五、六）。一旦出现即视为回归。
FORBIDDEN_METADATA_LABELS = (
    "SHA256",
    "File Size",
    "Part",
    "Total Parts",
    "Original Filename",
    "Normalized Filename",
    "命中方式",
)


@dataclass
class NotionUploadResult:
    """一次 Notion 上传的结果。"""

    ok: bool
    skipped: bool = False
    page_id: str = ""
    block_id: str = ""
    message: str = ""
    uploaded_parts: int = 0
    title: str = ""


def _text_block(text: str, *, bold: bool = False) -> dict[str, Any]:
    annotations = {"bold": True} if bold else {}
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}, "annotations": annotations}]
        },
    }


def _file_block(label: str, upload_id: str) -> list[dict[str, Any]]:
    return [
        _text_block(label, bold=True),
        {
            "object": "block",
            "type": "file",
            "file": {"type": "file_upload", "file_upload": {"id": upload_id}},
        },
    ]


def company_page_title(ticker: str, company: str) -> str:
    """公司页面标题：``Ticker｜Company``（需求四）。"""
    ticker = (ticker or "Unknown").strip()
    company = (company or "Unknown").strip()
    return f"{ticker}{COMPANY_TITLE_SEPARATOR}{company}"


def report_letter(letter: str) -> str:
    """规范化候选文件的序号字母（``A`` / ``B`` / ``AA``）。"""
    return re.sub(r"[^A-Z]", "", str(letter or "").upper())


def _report_suffix(letter: str) -> str:
    """文件页面标题后缀；首个文件无后缀，后续用 ``｜A`` / ``｜B``（需求八）。

    规则稳定、可预测：同一天同一公司的第一个文件不加后缀，之后按规范化文件名
    里的序号字母追加，因此多个 ``Gemini｜SK海力士`` 不会冲突。既不依赖 SHA256，
    也不暴露技术信息。
    """
    letter = report_letter(letter)
    return f"{COMPANY_TITLE_SEPARATOR}{letter}" if letter else ""


def report_page_title(record: ResearchFileRecord, *, letter: str = "", suffix: bool = False) -> str:
    """Notion 研究文件页面标题：``AI名称｜公司名称``（需求六、八）。"""
    ai_source = (record.ai_source or "Unknown").strip() or "Unknown"
    company = (record.company or "Unknown").strip() or "Unknown"
    title = f"{ai_source}{COMPANY_TITLE_SEPARATOR}{company}"
    return title + (_report_suffix(letter) if suffix else "")


def report_page_body(record: ResearchFileRecord) -> list[dict[str, Any]]:
    """极简页面正文（需求五、六）：日期 · Ticker 与「📎 研究报告」。

    正文**只**包含这两行说明；附件文件块由调用方追加。所有技术元数据
    （SHA256 / File Size / Part / Total Parts / 原始文件名 / 规范化文件名 /
    命中方式）都不再出现在 Notion 页面上。
    """
    meta_line = f"{record.date}{COMPANY_TITLE_SEPARATOR}{record.ticker}"
    return [_text_block(meta_line), _text_block("📎 研究报告")]


def _has_forbidden_labels(blocks: Sequence[dict[str, Any]]) -> bool:
    """校验正文块不含任何被禁用的元数据标签（测试与自检共用）。"""
    for block in blocks:
        if block.get("type") != "paragraph":
            continue
        for rich in block.get("paragraph", {}).get("rich_text", []):
            content = str(rich.get("text", {}).get("content", ""))
            for label in FORBIDDEN_METADATA_LABELS:
                if label in content:
                    return True
    return False


class ResearchNotionArchiver:
    """把候选研究成果上传到 Notion（复用现有 NotionClient）。"""

    def __init__(
        self,
        config: ResearchArchiveConfig,
        *,
        client: Any,
        root_page_id: str,
        store: Optional[ResearchArchiveStore] = None,
        max_upload_bytes: Optional[int] = None,
    ):
        # 不在构造时抛错：让上层能明确报「缺少凭据」而不是崩溃。
        self.config = config
        self.client = client
        self.root_page_id = root_page_id
        self.store = store
        self.max_upload_bytes = max_upload_bytes or config.max_upload_bytes

    @property
    def can_upload(self) -> bool:
        """是否具备上传条件（客户端 + 根页面）。"""
        return self.client is not None and bool(self.root_page_id)

    # ---------- 去重判断 ----------

    def should_upload(self, record: ResearchFileRecord, *, retry_failed: bool = False) -> tuple[bool, str]:
        """按 SHA-256 判断是否需要上传（需求二十四）。"""
        if self.store is None:
            return True, "无状态库，按需上传"
        existing = self.store.find_by_sha256(record.sha256)
        if existing is None:
            return True, "首次上传"
        if existing.status == STATUS_UPLOADED and existing.notion_page_id:
            return False, f"已上传（page={existing.notion_page_id[:8]}…），按 SHA256 跳过"
        if existing.status == "FAILED" and not retry_failed:
            return False, "上次上传失败，已按 SHA256 跳过（如需重试请使用 --retry-failed）"
        return True, "已有记录但未成功上传，继续上传"

    # ---------- 页面层级 ----------

    def find_or_create_category_page(self) -> str:
        """研究报告一级目录（固定在 Root 下，已存在则复用）。"""
        return self.client.find_or_create_child_page(self.root_page_id, RESEARCH_CATEGORY_TITLE)

    def find_or_create_date_page(self, category_page_id: str, date: str) -> str:
        """日期页创建在「研究报告」下面，并按日期从新到旧排序；同日只创建一个。"""
        position = None
        if hasattr(self.client, "date_page_position"):
            position = self.client.date_page_position(category_page_id, date)
        return self.client.find_or_create_child_page(category_page_id, date, position=position)

    def find_or_create_company_page(self, date_page_id: str, title: str) -> str:
        """公司页创建在日期页下面，已存在则复用。"""
        return self.client.find_or_create_child_page(date_page_id, title)

    def find_or_create_report_page(self, company_page_id: str, title: str) -> str:
        """研究文件页创建在公司页下面（每个文件一个独立页面）。"""
        return self.client.find_or_create_child_page(company_page_id, title)

    def _existing_report_titles(self, company_page_id: str) -> set[str]:
        """公司页下已存在的子页面标题（用于避免同日多文件标题冲突）。"""
        if not hasattr(self.client, "child_pages"):
            return set()
        return {str(item.get("title", "")).strip() for item in self.client.child_pages(company_page_id)}

    def report_page_title_for(
        self,
        record: ResearchFileRecord,
        *,
        letter: str = "",
        company_page_id: str = "",
    ) -> str:
        """解析最终页面标题：同一 AI 的多个文件自动追加 ``｜A`` / ``｜B``（需求八）。"""
        base = report_page_title(record)
        suffix = _report_suffix(letter)
        if not suffix:
            # 没有序号（异常情况）时保持基础标题，避免产生无意义的空后缀。
            return base
        existing = self._existing_report_titles(company_page_id) if company_page_id else set()
        # 首个文件（带字母时）不加后缀；若基础标题已被占用，则用序号字母区分。
        if base not in existing:
            return base
        candidate = base + suffix
        existing.add(candidate)
        return candidate

    # ---------- 上传 ----------

    def upload_candidate(
        self,
        candidate: ResearchCandidate,
        *,
        parts: Sequence[Path] | None = None,
        retry_failed: bool = False,
        dry_run: Optional[bool] = None,
    ) -> NotionUploadResult:
        """上传一个候选（及其分片）到独立的研究文件页面。"""
        dry_run = self.config.dry_run if dry_run is None else dry_run
        files = list(parts) if parts else [candidate.archive_path]
        total_parts = max(1, len(files))
        record = candidate.record(status="ARCHIVED")

        if self.store is not None:
            existing = self.store.find_by_sha256(candidate.sha256)
            if existing is not None:
                record.notion_page_id = existing.notion_page_id
                record.status = existing.status

        should, reason = self.should_upload(record, retry_failed=retry_failed)
        if not should:
            return NotionUploadResult(ok=True, skipped=True, message=reason,
                                      page_id=record.notion_page_id)

        if dry_run:
            return NotionUploadResult(
                ok=True, skipped=True,
                message=(
                    f"DRY-RUN：将上传 {len(files)} 个文件 → "
                    f"Root/{RESEARCH_CATEGORY_TITLE}/{candidate.date}/"
                    f"{company_page_title(candidate.ticker, candidate.company)}"
                ),
            )

        if self.client is None:
            return NotionUploadResult(ok=False, message="缺少 NotionClient")
        if not self.root_page_id:
            return NotionUploadResult(ok=False, message="缺少 NOTION_ROOT_PAGE_ID")

        try:
            assert self.client is not None
            self.client.retrieve_page(self.root_page_id)
            category_page_id = self.find_or_create_category_page()
            date_page_id = self.find_or_create_date_page(category_page_id, candidate.date)
            company_page_id = self.find_or_create_company_page(
                date_page_id, company_page_title(candidate.ticker, candidate.company)
            )
            title = self.report_page_title_for(
                record, letter=candidate.letter, company_page_id=company_page_id
            )
            report_page_id = self.find_or_create_report_page(company_page_id, title)

            blocks: list[dict[str, Any]] = list(report_page_body(record))
            upload_ids: list[str] = []
            for file_path in files:
                content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                upload_ids.append(self.client.upload_file(file_path, content_type=content_type))
            for index, upload_id in enumerate(upload_ids, start=1):
                label = (
                    "研究报告附件" if total_parts == 1
                    else f"研究报告附件（Part {index}/{total_parts}）"
                )
                blocks.extend(_file_block(label, upload_id))

            self.client.append_blocks(report_page_id, blocks)
            return NotionUploadResult(
                ok=True, page_id=report_page_id, message="上传成功",
                uploaded_parts=len(files), title=title,
            )
        except Exception as exc:
            logger.error("研究成果上传 Notion 失败 %s: %s", candidate.path, exc)
            return NotionUploadResult(ok=False, message=str(exc))

    def as_dict(self) -> dict:
        return {
            "root_page_id": self.root_page_id,
            "category_page_title": RESEARCH_CATEGORY_TITLE,
            "max_upload_bytes": self.max_upload_bytes,
            "dry_run": self.config.dry_run,
        }


def build_archiver(
    config: ResearchArchiveConfig,
    *,
    client: Any = None,
    root_page_id: str = "",
    store: Optional[ResearchArchiveStore] = None,
) -> ResearchNotionArchiver:
    """工厂：默认复用现有 ``load_notion_config()`` 读取 Token / Root Page。"""
    if not root_page_id:
        from ..notion_sync import load_notion_config

        notion = load_notion_config()
        root_page_id = notion["root_page_id"]
        max_upload_bytes = notion["max_upload_bytes"]
        if client is None and not config.dry_run and notion["token"]:
            from ..notion_sync import NotionClient

            client = NotionClient(notion["token"])
    else:
        max_upload_bytes = config.max_upload_bytes
    return ResearchNotionArchiver(
        config, client=client, root_page_id=root_page_id, store=store,
        max_upload_bytes=max_upload_bytes,
    )
