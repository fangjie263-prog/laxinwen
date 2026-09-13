"""AI Research Archive 的 SQLite 状态 / 去重库（独立于新闻库）。

为什么单独一个库（``data/research/research_archive.db``）：

- 新闻库 ``data/news.db`` 的 schema 面向文章与 AI 分析，本功能的记录形态完全不同；
- 独立库让「不破坏已有功能」变成结构性保证，不需要改动 ``storage.py``；
- 但**复用同一个显式事务 / WAL 约定**，与 ``Storage`` 保持一致风格。

去重核心是 ``sha256``（需求二十二、二十三），文件名 / 路径 / 大小都不是判据。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# 处理状态（需求三十二）
STATUS_NEW = "NEW"
STATUS_ARCHIVED = "ARCHIVED"
STATUS_SPLIT = "SPLIT"
STATUS_UPLOADED = "UPLOADED"
STATUS_FAILED = "FAILED"
STATUS_DUPLICATE = "DUPLICATE"
STATUSES = (
    STATUS_NEW, STATUS_ARCHIVED, STATUS_SPLIT,
    STATUS_UPLOADED, STATUS_FAILED, STATUS_DUPLICATE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256              TEXT    NOT NULL UNIQUE,
    original_filename   TEXT    NOT NULL DEFAULT '',
    normalized_filename TEXT    NOT NULL DEFAULT '',
    original_path       TEXT    NOT NULL DEFAULT '',
    archive_path        TEXT    NOT NULL DEFAULT '',
    date                TEXT    NOT NULL DEFAULT '',
    ticker              TEXT    NOT NULL DEFAULT 'Unknown',
    company             TEXT    NOT NULL DEFAULT 'Unknown',
    ai_source           TEXT    NOT NULL DEFAULT 'Unknown',
    ai_matched_by       TEXT    NOT NULL DEFAULT '',
    file_type           TEXT    NOT NULL DEFAULT '',
    file_size           INTEGER NOT NULL DEFAULT 0,
    part                INTEGER NOT NULL DEFAULT 1,
    total_parts         INTEGER NOT NULL DEFAULT 1,
    notion_page_id      TEXT    NOT NULL DEFAULT '',
    notion_block_id     TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'NEW',
    error               TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT '',
    processed_at        TEXT,
    uploaded_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_files_date   ON research_files(date);
CREATE INDEX IF NOT EXISTS idx_research_files_ticker ON research_files(ticker);
CREATE INDEX IF NOT EXISTS idx_research_files_ai     ON research_files(ai_source);
CREATE INDEX IF NOT EXISTS idx_research_files_status ON research_files(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_files_archive_path
    ON research_files(archive_path) WHERE archive_path <> '';
"""


# 终态：不允许被非终态记录降级覆盖（DUPLICATE 只是「同内容的另一份文件」，
# 不是已有主记录的终态，因此不在此列）。
_TERMINAL_STATUSES = {STATUS_UPLOADED, STATUS_SPLIT}


def _is_terminal(status: str) -> bool:
    return str(status or "") in _TERMINAL_STATUSES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 SHA-256（去重唯一判据）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass
class ResearchFileRecord:
    """``research_files`` 一行。"""

    sha256: str
    original_filename: str = ""
    normalized_filename: str = ""
    original_path: str = ""
    archive_path: str = ""
    date: str = ""
    ticker: str = "Unknown"
    company: str = "Unknown"
    ai_source: str = "Unknown"
    ai_matched_by: str = ""
    file_type: str = ""
    file_size: int = 0
    part: int = 1
    total_parts: int = 1
    notion_page_id: str = ""
    notion_block_id: str = ""
    status: str = STATUS_NEW
    error: str = ""
    created_at: str = field(default_factory=_now_iso)
    processed_at: Optional[str] = None
    uploaded_at: Optional[str] = None
    id: Optional[int] = None

    @property
    def uploaded(self) -> bool:
        """是否已经成功上传 Notion（用于「不重复上传」判断）。"""
        return bool(self.notion_page_id) and self.status in {STATUS_UPLOADED, STATUS_SPLIT}


class ResearchArchiveStore:
    """研究归档状态库。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResearchArchiveStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- 查询 ----------

    def find_by_sha256(self, sha256: str) -> Optional[ResearchFileRecord]:
        row = self._conn.execute(
            "SELECT * FROM research_files WHERE sha256 = ?", (str(sha256 or ""),)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_archive_path(self, archive_path: str | Path) -> Optional[ResearchFileRecord]:
        row = self._conn.execute(
            "SELECT * FROM research_files WHERE archive_path = ?", (str(archive_path or ""),)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def used_letters(self, *, date: str, company_dir: str) -> set[str]:
        """某「日期 + 公司」目录已占用的序号字母（用于独立编号）。"""
        rows = self._conn.execute(
            "SELECT normalized_filename FROM research_files WHERE date = ? AND archive_path LIKE ?",
            (date, f"%/{company_dir}/%"),
        ).fetchall()
        import re

        letters: set[str] = set()
        for row in rows:
            match = re.match(r"^\d{8}([A-Z]+)_", str(row["normalized_filename"] or ""))
            if match:
                letters.add(match.group(1))
        return letters

    def list_records(
        self,
        *,
        status: Optional[str] = None,
        ticker: Optional[str] = None,
        ai_source: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 100,
    ) -> list[ResearchFileRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status), ("ticker", ticker),
            ("ai_source", ai_source), ("date", date),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM research_files"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date DESC, id ASC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS total FROM research_files GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    # ---------- 写入 ----------

    def upsert(self, record: ResearchFileRecord) -> ResearchFileRecord:
        """按 ``sha256`` 幂等写入（已存在则更新可变字段）。

        注意：``sha256`` 相同即视为**同一份内容**，因此不会重复写新行，
        也不会重复上传 Notion。
        """
        with self._tx() as conn:
            existed = conn.execute(
                "SELECT id, status FROM research_files WHERE sha256 = ?", (record.sha256,)
            ).fetchone()
            if existed is None:
                cursor = conn.execute(
                    """
                    INSERT INTO research_files (
                        sha256, original_filename, normalized_filename, original_path,
                        archive_path, date, ticker, company, ai_source, ai_matched_by,
                        file_type, file_size, part, total_parts, notion_page_id,
                        notion_block_id, status, error, created_at, processed_at, uploaded_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.sha256, record.original_filename, record.normalized_filename,
                        record.original_path, record.archive_path, record.date,
                        record.ticker, record.company, record.ai_source, record.ai_matched_by,
                        record.file_type, int(record.file_size), int(record.part),
                        int(record.total_parts), record.notion_page_id, record.notion_block_id,
                        record.status, record.error, record.created_at,
                        record.processed_at, record.uploaded_at,
                    ),
                )
                record.id = int(cursor.lastrowid or 0)
                return record

            record.id = int(existed["id"])
            # 关键保护：一条 sha256 记录一旦「已成功上传」或处于终态，
            # 不允许被后续路径（例如重复文件落库）降级覆盖，否则会丢失
            # notion_page_id，导致重复上传或永久跳过（需求二十四 / 三十二）。
            if _is_terminal(existed["status"]) and not _is_terminal(record.status):
                return record
            conn.execute(
                """
                UPDATE research_files SET
                    original_filename = ?, normalized_filename = ?, original_path = ?,
                    archive_path = ?, date = ?, ticker = ?, company = ?, ai_source = ?,
                    ai_matched_by = ?, file_type = ?, file_size = ?, part = ?, total_parts = ?,
                    notion_page_id = ?, notion_block_id = ?, status = ?, error = ?,
                    processed_at = ?, uploaded_at = ?
                WHERE sha256 = ?
                """,
                (
                    record.original_filename, record.normalized_filename, record.original_path,
                    record.archive_path, record.date, record.ticker, record.company,
                    record.ai_source, record.ai_matched_by, record.file_type,
                    int(record.file_size), int(record.part), int(record.total_parts),
                    record.notion_page_id, record.notion_block_id, record.status,
                    record.error, record.processed_at, record.uploaded_at, record.sha256,
                ),
            )
        return record

    def mark(
        self,
        sha256: str,
        *,
        status: Optional[str] = None,
        error: Optional[str] = None,
        notion_page_id: Optional[str] = None,
        notion_block_id: Optional[str] = None,
        archive_path: Optional[str] = None,
        total_parts: Optional[int] = None,
    ) -> None:
        updates: list[str] = []
        params: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if notion_page_id is not None:
            updates.append("notion_page_id = ?")
            params.append(notion_page_id)
            updates.append("uploaded_at = ?")
            params.append(_now_iso())
        if notion_block_id is not None:
            updates.append("notion_block_id = ?")
            params.append(notion_block_id)
        if archive_path is not None:
            updates.append("archive_path = ?")
            params.append(archive_path)
        if total_parts is not None:
            updates.append("total_parts = ?")
            params.append(int(total_parts))
        updates.append("processed_at = ?")
        params.append(_now_iso())
        params.append(sha256)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE research_files SET {', '.join(updates)} WHERE sha256 = ?", params
            )

    def mark_failed(self, sha256: str, error: str) -> None:
        """失败记录错误原因；**不会**无限重复上传（由上层 status 判断控制）。"""
        self.mark(sha256, status=STATUS_FAILED, error=str(error)[:2000])

    def relocate(self, old_path: str | Path, new_path: str | Path, *, ticker: str = "", company: str = "") -> None:
        """Synchronize a historical archive move without changing upload state."""
        updates = ["archive_path = ?"]
        params: list[Any] = [str(new_path)]
        if ticker:
            updates.append("ticker = ?")
            params.append(ticker)
        if company:
            updates.append("company = ?")
            params.append(company)
        params.append(str(old_path))
        with self._tx() as conn:
            conn.execute(
                f"UPDATE research_files SET {', '.join(updates)} WHERE archive_path = ?",
                params,
            )

    # ---------- 行转换 ----------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ResearchFileRecord:
        return ResearchFileRecord(
            id=int(row["id"]),
            sha256=str(row["sha256"]),
            original_filename=str(row["original_filename"] or ""),
            normalized_filename=str(row["normalized_filename"] or ""),
            original_path=str(row["original_path"] or ""),
            archive_path=str(row["archive_path"] or ""),
            date=str(row["date"] or ""),
            ticker=str(row["ticker"] or "Unknown"),
            company=str(row["company"] or "Unknown"),
            ai_source=str(row["ai_source"] or "Unknown"),
            ai_matched_by=str(row["ai_matched_by"] or ""),
            file_type=str(row["file_type"] or ""),
            file_size=int(row["file_size"] or 0),
            part=int(row["part"] or 1),
            total_parts=int(row["total_parts"] or 1),
            notion_page_id=str(row["notion_page_id"] or ""),
            notion_block_id=str(row["notion_block_id"] or ""),
            status=str(row["status"] or STATUS_NEW),
            error=str(row["error"] or ""),
            created_at=str(row["created_at"] or ""),
            processed_at=row["processed_at"],
            uploaded_at=row["uploaded_at"],
        )
