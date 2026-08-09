"""SQLite 存储层 —— 第一版唯一事实来源。

JSONL / Markdown 都是从此处派生的导出文件。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .model import Article

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT    NOT NULL,
    source_name    TEXT    NOT NULL,
    canonical_url  TEXT    NOT NULL,
    title          TEXT    NOT NULL,
    authors        TEXT    NOT NULL DEFAULT '[]',
    published_at   TEXT,
    discovered_at  TEXT    NOT NULL,
    fetched_at     TEXT,
    body_text      TEXT    NOT NULL DEFAULT '',
    body_html      TEXT,
    images         TEXT    NOT NULL DEFAULT '[]',
    lead_image     TEXT,
    language       TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'new',
    title_fp       TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_canonical_url
    ON articles(canonical_url);
CREATE INDEX IF NOT EXISTS idx_articles_published_at
    ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_title_fp
    ON articles(title_fp);
CREATE INDEX IF NOT EXISTS idx_articles_source
    ON articles(source_id);
"""


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class Storage:
    """SQLite 数据库访问封装（标准库 sqlite3，无 ORM）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---------- 写入 ----------

    def insert_article(self, article: Article, title_fp: str = "") -> tuple[int, bool]:
        """插入文章。返回 (rowid, inserted)：
        - 若 canonical_url 已存在（唯一约束冲突），返回 (已存在行id, False)。
        """
        row = (
            article.source_id,
            article.source_name,
            article.canonical_url,
            article.title,
            __import__("json").dumps(article.authors, ensure_ascii=False),
            _to_iso(article.published_at),
            _to_iso(article.discovered_at),
            _to_iso(article.fetched_at),
            article.body_text,
            article.body_html,
            __import__("json").dumps(article.images, ensure_ascii=False),
            article.lead_image,
            article.language,
            article.status,
            title_fp,
        )
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    """INSERT INTO articles
                       (source_id, source_name, canonical_url, title, authors,
                        published_at, discovered_at, fetched_at, body_text,
                        body_html, images, lead_image, language, status, title_fp)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                return int(cur.lastrowid), True
        except sqlite3.IntegrityError as exc:
            if "canonical_url" in str(exc):
                # 已存在，查询原行 id
                with self._tx() as conn:
                    cur = conn.execute(
                        "SELECT id FROM articles WHERE canonical_url = ?",
                        (article.canonical_url,),
                    )
                    row = cur.fetchone()
                    existing_id = int(row["id"]) if row else 0
                return existing_id, False
            raise

    def update_article_body(
        self,
        article_id: int,
        *,
        title: str,
        authors: list[str],
        body_text: str,
        body_html: Optional[str],
        images: list[str],
        lead_image: Optional[str],
        published_at: Optional[datetime],
        fetched_at: Optional[datetime],
        language: str,
        status: str,
    ) -> None:
        """文章抓取完成后回填正文等字段。"""
        with self._tx() as conn:
            conn.execute(
                """UPDATE articles SET
                     title=?, authors=?, body_text=?, body_html=?, images=?,
                     lead_image=?, published_at=?, fetched_at=?, language=?, status=?
                   WHERE id=?""",
                (
                    title,
                    __import__("json").dumps(authors, ensure_ascii=False),
                    body_text,
                    body_html,
                    __import__("json").dumps(images, ensure_ascii=False),
                    lead_image,
                    _to_iso(published_at),
                    _to_iso(fetched_at),
                    language,
                    status,
                    article_id,
                ),
            )

    def mark_failed(self, article_id: int, *, error: str) -> None:
        """记录抓取失败。error 存入 body_text 前缀（轻量做法）。"""
        with self._tx() as conn:
            conn.execute(
                "UPDATE articles SET status='failed' WHERE id=?",
                (article_id,),
            )
            conn.execute(
                "UPDATE articles SET body_text=? WHERE id=? AND body_text=''",
                (f"[抓取失败] {error}", article_id),
            )

    # ---------- 查询 ----------

    def get_article(self, article_id: int) -> Optional[Article]:
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM articles WHERE id=?", (article_id,)
            ).fetchone()
        return self._row_to_article(row) if row else None

    def list_articles(
        self,
        *,
        limit: int = 50,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[Article]:
        sql = "SELECT * FROM articles"
        where: list[str] = []
        params: list = []
        if source_id:
            where.append("source_id = ?")
            params.append(source_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(published_at, discovered_at) DESC LIMIT ?"
        params.append(limit)
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_article(r) for r in rows]

    def count(self, source_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM articles"
        params: list = []
        if source_id:
            sql += " WHERE source_id = ?"
            params.append(source_id)
        with self._conn:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"])

    def count_by_status(self, source_id: Optional[str] = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS c FROM articles"
        params: list = []
        if source_id:
            sql += " WHERE source_id = ?"
            params.append(source_id)
        sql += " GROUP BY status"
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return {r["status"]: int(r["c"]) for r in rows}

    def url_exists(self, canonical_url: str) -> bool:
        with self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM articles WHERE canonical_url = ?", (canonical_url,)
            ).fetchone()
        return row is not None

    def title_fp_exists(self, source_id: str, title_fp: str) -> bool:
        """同源是否存在相同标题指纹（第二层去重）。"""
        if not title_fp:
            return False
        with self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM articles WHERE source_id = ? AND title_fp = ?",
                (source_id, title_fp),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> Article:
        import json

        return Article(
            id=int(row["id"]),
            source_id=row["source_id"],
            source_name=row["source_name"],
            canonical_url=row["canonical_url"],
            title=row["title"],
            authors=json.loads(row["authors"] or "[]"),
            published_at=_parse_iso(row["published_at"]),
            discovered_at=_parse_iso(row["discovered_at"]) or datetime.now(timezone.utc),
            fetched_at=_parse_iso(row["fetched_at"]),
            body_text=row["body_text"] or "",
            body_html=row["body_html"],
            images=json.loads(row["images"] or "[]"),
            lead_image=row["lead_image"],
            language=row["language"] or "",
            status=row["status"],
        )
