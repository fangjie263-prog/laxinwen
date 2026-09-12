"""归档主流程：安全优先，先归档副本、再写库、最后才移动原文件。

严格执行需求三十三的次序（**绝不先删除**）::

    Inbox 文件
      ↓ 1. 计算 sha256 + 查库去重
      ↓ 2. dry-run? → 只输出计划，结束
      ↓ 3. 复制到 Archive（同盘用 link→copy 回退，保留原文件）
      ↓ 4. 校验归档副本（大小 + 可解析）
      ↓ 5. 写数据库（NEW → ARCHIVED / SPLIT）
      ↓ 6. 超限则切割（PDF 按页 / DOCX 按段 / HTML 按 section）
      ↓ 7. 校验分片
      ↓ 8. 上传 Notion（复用 NotionClient，按 sha256 跳过重复）
      ↓ 9. 全部成功后才把原文件移出 Inbox（移动失败不丢数据）

任何一步失败：**不删除**原文件，记录错误原因，可选地移动到 ``Failed``。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .archive_store import (
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_SPLIT,
    STATUS_UPLOADED,
    ResearchArchiveStore,
)
from .config import ResearchArchiveConfig
from .notion_archive import ResearchNotionArchiver, NotionUploadResult, build_archiver
from .sanitize import MAX_ARCHIVE_FILE_SIZE
from .scanner import ResearchArchiveScanner, ResearchCandidate
from .splitter import split_file, verify_part

logger = logging.getLogger("news.research.archive")


@dataclass
class ResearchArchiveResult:
    """一次归档运行的结果汇总。"""

    scanned: int = 0
    new: int = 0
    duplicates: int = 0
    archived: int = 0
    split: int = 0
    uploaded: int = 0
    upload_skipped: int = 0
    failed: int = 0
    moved: int = 0
    dry_run: bool = True
    candidates: list[ResearchCandidate] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "new": self.new,
            "duplicates": self.duplicates,
            "archived": self.archived,
            "split": self.split,
            "uploaded": self.uploaded,
            "upload_skipped": self.upload_skipped,
            "failed": self.failed,
            "moved": self.moved,
            "dry_run": self.dry_run,
        }


def _copy_archive(source: Path, destination: Path) -> None:
    """复制归档副本（先写临时文件再原子替换，避免半成品）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # 不允许覆盖已有**不同内容**文件（需求十一）
        if destination.stat().st_size == source.stat().st_size:
            return
        raise FileExistsError(f"目标文件已存在且大小不同，拒绝覆盖：{destination}")
    temp = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temp)
    os.replace(temp, destination)


def _move_out_of_inbox(source: Path, failed_dir: Path) -> Optional[Path]:
    """把原文件移出 Inbox（优先移到 Failed，失败时返回 None 但保留原文件）。"""
    destination = failed_dir / source.name
    counter = 2
    while destination.exists():
        destination = failed_dir / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return destination
    except OSError as exc:
        logger.error("移动原文件失败（原文件保留在 Inbox）：%s → %s（%s）", source, destination, exc)
        return None


def run_research_archive(
    *,
    config: ResearchArchiveConfig,
    store: Optional[ResearchArchiveStore] = None,
    scanner: Optional[ResearchArchiveScanner] = None,
    archiver: Optional[ResearchNotionArchiver] = None,
    client: object = None,
    root_page_id: str = "",
    retry_failed: bool = False,
    move_on_success: bool = True,
    on_message: Optional[Callable[[str], None]] = None,
) -> ResearchArchiveResult:
    """执行一次归档。

    参数都可注入（测试友好）：``store`` / ``scanner`` / ``archiver`` / ``client``。
    """
    result = ResearchArchiveResult(dry_run=config.dry_run)

    def emit(message: str) -> None:
        logger.info("%s", message)
        result.messages.append(message)
        if on_message is not None:
            try:
                on_message(message)
            except Exception:  # pragma: no cover - 回调不应影响主流程
                logger.debug("on_message 回调异常", exc_info=True)

    own_store = store is None
    store = store or ResearchArchiveStore(config.db_path)
    try:
        scanner = scanner or ResearchArchiveScanner(config, store=store)
        candidates = scanner.scan()
        result.candidates = candidates
        result.scanned = len(candidates)
        result.new = sum(1 for item in candidates if not item.duplicate)
        result.duplicates = result.scanned - result.new

        if config.dry_run:
            for line in scanner.format_plan(candidates):
                emit(line)
            return result

        if archiver is None:
            archiver = build_archiver(
                config, client=client, root_page_id=root_page_id, store=store
            )

        if not archiver.can_upload and candidates:
            # 明确失败而不是「静默归档但没上传」，避免用户以为已经同步到 Notion。
            for line in scanner.format_plan(candidates):
                emit(line)
            emit(
                "ERROR · 缺少 Notion 凭据（NOTION_TOKEN / NOTION_ROOT_PAGE_ID 或 NotionClient），"
                "APPLY 已中止；未移动、未上传任何文件"
            )
            result.failed = max(1, result.new)
            return result

        for candidate in candidates:
            existing = store.find_by_sha256(candidate.sha256) if store is not None else None
            retryable = bool(
                retry_failed
                and existing is not None
                and existing.status == STATUS_FAILED
            )
            if candidate.duplicate and not retryable:
                result.messages.append(
                    f"SKIP DUPLICATE · {candidate.path.name} · sha256={candidate.sha256[:16]}…"
                    f"（已存在：{candidate.duplicate_of}）"
                )
                if store is not None and existing is not None:
                    # 已有主记录（可能已上传）不允许被重复文件覆盖成 DUPLICATE，
                    # 否则会丢失 notion_page_id，导致重复上传或永久跳过。
                    if existing.status in {STATUS_UPLOADED, STATUS_SPLIT}:
                        logger.info(
                            "重复文件（不覆盖主记录）%s ← 已存在 %s",
                            candidate.path.name, existing.archive_path,
                        )
                    else:
                        store.upsert(candidate.record(status=STATUS_DUPLICATE))
                continue
            if candidate.duplicate and retryable:
                result.messages.append(
                    f"RETRY FAILED · {candidate.path.name} · sha256={candidate.sha256[:16]}…"
                )
                result.duplicates += 1
                result.new -= 1

            outcome = _process_candidate(
                candidate, config=config, store=store, archiver=archiver,
                retry_failed=retry_failed, move_on_success=move_on_success, emit=emit,
            )
            if outcome == STATUS_FAILED:
                result.failed += 1
            elif outcome == "skipped":
                result.upload_skipped += 1
            elif outcome == STATUS_SPLIT:
                result.split += 1
                result.archived += 1
                result.moved += 1
            elif outcome == STATUS_UPLOADED:
                result.uploaded += 1
                result.archived += 1
                result.moved += 1
            else:
                result.archived += 1
                result.moved += 1
        return result
    finally:
        if own_store:
            store.close()


def _process_candidate(
    candidate: ResearchCandidate,
    *,
    config: ResearchArchiveConfig,
    store: ResearchArchiveStore,
    archiver: ResearchNotionArchiver,
    retry_failed: bool,
    move_on_success: bool,
    emit: Callable[[str], None],
) -> str:
    """处理单个候选；返回最终状态字符串（或 ``skipped``）。"""
    max_bytes = min(config.max_upload_bytes, MAX_ARCHIVE_FILE_SIZE)
    record = candidate.record(status="NEW")
    store.upsert(record)
    emit(f"NEW · {candidate.path.name} · sha256={candidate.sha256[:16]}…")

    parts: list[Path] = [candidate.archive_path]
    try:
        # 1) 归档副本（先复制，绝不动原文件）
        _copy_archive(candidate.path, candidate.archive_path)
        if not candidate.archive_path.is_file():
            raise RuntimeError(f"归档副本未生成：{candidate.archive_path}")

        # 2) 校验归档副本可解析
        if not verify_part(candidate.archive_path, candidate.extension):
            raise RuntimeError(f"归档副本校验失败（内容不可解析）：{candidate.archive_path}")

        # 3) 超限则按类型正确切割
        if candidate.size > max_bytes:
            split_result = split_file(
                candidate.path, candidate.archive_dir,
                stem=_stem_of(candidate.normalized_name), max_bytes=max_bytes,
            )
            if not split_result.ok:
                raise RuntimeError(split_result.error or "切割失败")
            invalid = [path for path in split_result.parts if not verify_part(path, candidate.extension)]
            if invalid:
                raise RuntimeError(f"分片校验失败：{[path.name for path in invalid]}")
            parts = list(split_result.parts)
            record.status = STATUS_SPLIT
            record.total_parts = len(parts)
            record.error = ""
            store.upsert(record)
            emit(
                f"SPLIT · {candidate.path.name} · {candidate.size} bytes → "
                f"{len(parts)} 片（≤{max_bytes}）"
            )

        # 4) 上传 Notion（按 sha256 跳过重复）
        upload: NotionUploadResult = archiver.upload_candidate(
            candidate, parts=parts, retry_failed=retry_failed, dry_run=False
        )
        if not upload.ok:
            store.mark_failed(candidate.sha256, upload.message)
            emit(f"UPLOAD FAILED · {candidate.path.name} · {upload.message}")
            _move_out_of_inbox(candidate.path, config.failed_dir)
            return STATUS_FAILED

        if upload.skipped:
            emit(f"UPLOAD SKIP · {candidate.path.name} · {upload.message}")

        final_status = record.status if record.status == STATUS_SPLIT else STATUS_UPLOADED
        store.mark(
            candidate.sha256,
            status=final_status,
            error="",
            notion_page_id=upload.page_id or None,
            total_parts=len(parts),
        )
        emit(
            f"{'UPLOADED' if not upload.skipped else 'ARCHIVED'} · {candidate.path.name} · "
            f"→ {candidate.archive_path}（{len(parts)} 片）"
        )

        # 5) 全部成功后才移动原文件（移动失败不丢数据）
        if move_on_success:
            if _archive_origin(candidate):
                emit(f"INBOX 已清理 · {candidate.path.name}")
            else:
                emit(f"INBOX 保留（移动失败，可重试）· {candidate.path.name}")
        return final_status
    except Exception as exc:
        message = str(exc)
        logger.error("归档失败 %s: %s", candidate.path, exc)
        store.mark_failed(candidate.sha256, message)
        emit(f"FAILED · {candidate.path.name} · {message}")
        if config.dry_run is False:
            _move_out_of_inbox(candidate.path, config.failed_dir)
        return STATUS_FAILED


def _stem_of(normalized_name: str) -> str:
    return Path(normalized_name).stem


def _archive_origin(candidate: ResearchCandidate) -> bool:
    """把原文件从 Inbox 移入归档目录（成功归档后）。

    与需求三十三一致：**先复制归档 → 校验 → 写库 → 上传成功**，最后才移动。
    移动采用「删除 Inbox 里的原件」而不是剪切归档副本，避免破坏归档路径。
    """
    try:
        candidate.path.unlink()
        return True
    except OSError as exc:
        logger.error("清理 Inbox 原文件失败（数据未丢失）：%s（%s）", candidate.path, exc)
        return False
