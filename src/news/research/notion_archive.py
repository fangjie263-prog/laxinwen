"""把研究成果写入 Notion —— **复用现有 NotionClient / 认证 / state**。

硬约束（需求二十八、二十九、二十四）：

- 不新建第二套 Token、第二套 Client、第二套 Scheduler；
  ``NotionClient`` 直接从 ``news.notion_sync`` 导入，Token / Root Page 走同一个
  ``load_notion_config()``（``NOTION_TOKEN`` / ``NOTION_ROOT_PAGE_ID``）；
- 页面结构复用现有约定 ``Root → <日期> → <公司>``，日期页用现有
  ``date_page_position()`` 排序；
- 每次上传前先查 **SHA-256**：已归档且已成功上传则**跳过**，不重复上传；
- 上传失败时记录 ``notion_page_id`` 为空 + ``FAILED``，不会无限重试（同一 sha256
  已有失败记录且未变化时默认跳过重试，除非 ``--retry-failed``）。

每个分片作为独立的 Notion file block 追加到同一公司页面的“研究成果”块里，
文件名即上表中的 ``Normalized Filename``，元数据以 ``键: 值`` 的段落形式给出，
使 Notion 侧可直接按 Ticker / AI Source / Date 检索。
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .archive_store import ResearchFileRecord, ResearchArchiveStore, STATUS_UPLOADED
from .config import ResearchArchiveConfig
from .scanner import ResearchCandidate

logger = logging.getLogger(__name__)


@dataclass
class NotionUploadResult:
    """一次 Notion 上传的结果。"""

    ok: bool
    skipped: bool = False
    page_id: str = ""
    block_id: str = ""
    message: str = ""
    uploaded_parts: int = 0


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


def _metadata_lines(record: ResearchFileRecord, *, part: int, total_parts: int) -> list[str]:
    """Notion 侧可检索的元数据（需求二十九）。"""
    return [
        f"Date: {record.date}",
        f"Ticker: {record.ticker}",
        f"Company: {record.company}",
        f"AI Source: {record.ai_source}",
        f"File Type: {record.file_type.upper()}",
        f"Original Filename: {record.original_filename}",
        f"Normalized Filename: {record.normalized_filename}",
        f"SHA256: {record.sha256}",
        f"File Size: {record.file_size}",
        f"Part: {part}",
        f"Total Parts: {total_parts}",
    ]


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

    # ---------- 上传 ----------

    def upload_candidate(
        self,
        candidate: ResearchCandidate,
        *,
        parts: Sequence[Path] | None = None,
        retry_failed: bool = False,
        dry_run: Optional[bool] = None,
    ) -> NotionUploadResult:
        """上传一个候选（及其分片）。"""
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
                message=f"DRY-RUN：将上传 {len(files)} 个文件 → Root/{candidate.date}/{candidate.company_dir}",
            )

        if self.client is None:
            return NotionUploadResult(ok=False, message="缺少 NotionClient")
        if not self.root_page_id:
            return NotionUploadResult(ok=False, message="缺少 NOTION_ROOT_PAGE_ID")

        try:
            assert self.client is not None
            self.client.retrieve_page(self.root_page_id)
            date_page_id = self.client.find_or_create_child_page(
                self.root_page_id, candidate.date,
                position=self.client.date_page_position(self.root_page_id, candidate.date),
            )
            company_page_id = self.client.find_or_create_child_page(
                date_page_id, candidate.company_dir
            )
            blocks: list[dict[str, Any]] = [
                _text_block(f"命中方式：{candidate.ai_matched_by or 'unknown'}"),
                *_text_block_lines(_metadata_lines(record, part=1, total_parts=total_parts)),
            ]
            for index, file_path in enumerate(files, start=1):
                content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                upload_id = self.client.upload_file(file_path, content_type=content_type)
                label = (
                    file_path.name if total_parts == 1
                    else f"{file_path.name}（Part {index}/{total_parts}）"
                )
                blocks.extend(_file_block(label, upload_id))
            self.client.append_blocks(company_page_id, blocks)
            return NotionUploadResult(
                ok=True, page_id=company_page_id, message="上传成功",
                uploaded_parts=len(files),
            )
        except Exception as exc:
            logger.error("研究成果上传 Notion 失败 %s: %s", candidate.path, exc)
            return NotionUploadResult(ok=False, message=str(exc))

    def as_dict(self) -> dict:
        return {
            "root_page_id": self.root_page_id,
            "max_upload_bytes": self.max_upload_bytes,
            "dry_run": self.config.dry_run,
        }


def _text_block_lines(lines: Sequence[str]) -> list[dict[str, Any]]:
    return [_text_block(line) for line in lines]


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
