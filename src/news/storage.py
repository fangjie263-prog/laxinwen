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

-- AI 分析结果表（第二阶段）
-- 唯一约束选择 (article_id, provider, model, prompt_version)：
-- 同一篇文章未来可用不同 provider/model/prompt 版本重新分析并共存。
CREATE TABLE IF NOT EXISTS article_analysis (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id              INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    provider                TEXT    NOT NULL DEFAULT 'openai-compatible',
    model                   TEXT    NOT NULL,
    prompt_version          TEXT    NOT NULL,
    summary_zh              TEXT    NOT NULL,
    key_points_json         TEXT    NOT NULL DEFAULT '[]',
    topics_json             TEXT    NOT NULL DEFAULT '[]',
    entities_json           TEXT    NOT NULL DEFAULT '[]',
    market_relevance        TEXT    NOT NULL,
    market_relevance_reason TEXT    NOT NULL,
    language                TEXT    NOT NULL DEFAULT '',
    status                  TEXT    NOT NULL DEFAULT 'success',
    error                   TEXT    NOT NULL DEFAULT '',
    usage_json              TEXT    NOT NULL DEFAULT '{}',
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_article_provider_model_pv
    ON article_analysis(article_id, provider, model, prompt_version);
CREATE INDEX IF NOT EXISTS idx_analysis_status
    ON article_analysis(status);
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

    def __enter__(self) -> "Storage":
        """支持 with 语句（GUI 等场景使用）。"""
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

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

    # ---------- AI 分析（第二阶段） ----------

    def upsert_analysis(
        self,
        *,
        article_id: int,
        provider: str,
        model: str,
        prompt_version: str,
        summary_zh: str,
        key_points: list[str],
        topics: list[str],
        entities: list[dict],
        market_relevance: str,
        market_relevance_reason: str,
        language: str,
        status: str = "success",
        error: str = "",
        usage: dict | None = None,
    ) -> int:
        """插入或更新一条 AI 分析记录。

        唯一约束 (article_id, provider, model, prompt_version)：
        同参数重新分析时覆盖；不同模型/版本可并存。
        返回记录 id。
        """
        import json

        now = datetime.now(timezone.utc).isoformat()
        row = (
            article_id,
            provider,
            model,
            prompt_version,
            summary_zh,
            json.dumps(key_points, ensure_ascii=False),
            json.dumps(topics, ensure_ascii=False),
            json.dumps(entities, ensure_ascii=False),
            market_relevance,
            market_relevance_reason,
            language,
            status,
            error,
            json.dumps(usage or {}, ensure_ascii=False),
            now,
            now,
        )
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO article_analysis
                   (article_id, provider, model, prompt_version, summary_zh,
                    key_points_json, topics_json, entities_json,
                    market_relevance, market_relevance_reason, language,
                    status, error, usage_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(article_id, provider, model, prompt_version)
                   DO UPDATE SET
                     summary_zh=excluded.summary_zh,
                     key_points_json=excluded.key_points_json,
                     topics_json=excluded.topics_json,
                     entities_json=excluded.entities_json,
                     market_relevance=excluded.market_relevance,
                     market_relevance_reason=excluded.market_relevance_reason,
                     language=excluded.language,
                     status=excluded.status,
                     error=excluded.error,
                     usage_json=excluded.usage_json,
                     updated_at=excluded.updated_at""",
                row,
            )
            cur = conn.execute(
                "SELECT id FROM article_analysis WHERE article_id=? AND provider=? AND model=? AND prompt_version=?",
                (article_id, provider, model, prompt_version),
            )
            found = cur.fetchone()
            return int(found["id"]) if found else 0

    def analysis_exists(
        self,
        article_id: int,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> bool:
        """判断是否已存在成功分析记录。

        不传 provider/model/prompt_version 时，只要任意一条成功记录即视为已分析。
        """
        sql = "SELECT 1 FROM article_analysis WHERE article_id=? AND status='success'"
        params: list = [article_id]
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        if model:
            sql += " AND model=?"
            params.append(model)
        if prompt_version:
            sql += " AND prompt_version=?"
            params.append(prompt_version)
        with self._conn:
            row = self._conn.execute(sql, params).fetchone()
        return row is not None

    def list_unanalyzed_articles(
        self,
        *,
        source_id: str | None = None,
        limit: int = 5,
        include_failed: bool = False,
    ) -> list[Article]:
        """列出需要 AI 处理的文章。

        条件：
        - 已成功抓取（status='fetched'）；
        - 没有对应的成功 AI 分析；
        - include_failed=False（默认）：跳过已有失败分析记录的文章（尚未分析过）；
        - include_failed=True（--retry-failed）：额外包含之前 AI 处理失败的文章，重新处理。
        """
        where = [
            "a.status='fetched'",
            "NOT EXISTS (SELECT 1 FROM article_analysis x WHERE x.article_id=a.id AND x.status='success')",
        ]
        params: list = []
        if source_id:
            where.append("a.source_id=?")
            params.append(source_id)
        if not include_failed:
            where.append(
                "NOT EXISTS (SELECT 1 FROM article_analysis f WHERE f.article_id=a.id AND f.status='failed')"
            )
        sql = (
            "SELECT a.* FROM articles a WHERE "
            + " AND ".join(where)
            + " ORDER BY COALESCE(a.published_at, a.discovered_at) DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_article(r) for r in rows]

    def count_analysis(
        self,
        *,
        source_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """统计 article_analysis 记录数。"""
        sql = (
            "SELECT COUNT(*) AS c FROM article_analysis x "
            "JOIN articles a ON a.id=x.article_id"
        )
        where: list[str] = []
        params: list = []
        if source_id:
            where.append("a.source_id=?")
            params.append(source_id)
        if status:
            where.append("x.status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"])

    def list_analysis(
        self,
        *,
        article_id: int | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """列出 AI 分析记录（供 CLI 展示）。"""
        sql = (
            "SELECT x.*, a.source_id, a.title FROM article_analysis x "
            "JOIN articles a ON a.id=x.article_id"
        )
        params: list = []
        if article_id:
            sql += " WHERE x.article_id=?"
            params.append(article_id)
        sql += " ORDER BY x.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return list(rows)

    def list_analysis_success(
        self,
        *,
        source_id: str | None = None,
        article_id: int | None = None,
        limit: int = 10**9,
    ) -> list[sqlite3.Row]:
        """列出 AI 分析成功（status='ok'/'success'）的记录，联表带出文章信息。

        用于 HTML 研究结果导出：只返回"成功分析"，失败记录不会出现在正常研究页面。
        兼容两代 status 约定：``ok``（PR #4 采用）与 ``success``（PR #3/#5 采用）。
        返回行包含 x.* 与 a.*（source_id/source_name/title/canonical_url/...）。
        """
        sql = (
            "SELECT x.*, a.source_id AS art_source_id, a.source_name, "
            "a.title AS art_title, a.canonical_url, a.published_at, "
            "a.discovered_at, a.body_text, a.language AS art_language, "
            "a.authors AS art_authors "
            "FROM article_analysis x JOIN articles a ON a.id=x.article_id "
            "WHERE x.status IN ('ok', 'success')"
        )
        params: list = []
        if source_id:
            sql += " AND a.source_id=?"
            params.append(source_id)
        if article_id is not None:
            sql += " AND x.article_id=?"
            params.append(article_id)
        sql += " ORDER BY COALESCE(a.published_at, a.discovered_at) DESC LIMIT ?"
        params.append(limit)
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return list(rows)

    def list_articles_with_analysis(
        self,
        *,
        source_id: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """列出文章，并附上每条文章的 AI 分析状态（用于 News Archive HTML）。

        返回行包含 articles.* 以及：
        - ``ai_status``：'ok' / 'failed' / None（无分析）
        - ``summary_zh`` / ``market_relevance``：若有成功分析则带出
        按发布日期倒序（COALESCE(published_at, discovered_at)）。
        """
        sql = (
            "SELECT a.*, "
            "(SELECT x.status FROM article_analysis x "
            " WHERE x.article_id=a.id AND x.status IN ('ok','success') "
            " ORDER BY x.updated_at DESC LIMIT 1) AS ai_status, "
            "(SELECT CASE WHEN EXISTS (SELECT 1 FROM article_analysis f "
            "   WHERE f.article_id=a.id AND f.status='failed') THEN 1 ELSE 0 END) AS ai_has_failed, "
            "(SELECT x.summary_zh FROM article_analysis x "
            " WHERE x.article_id=a.id AND x.status IN ('ok','success') "
            " ORDER BY x.updated_at DESC LIMIT 1) AS summary_zh, "
            "(SELECT x.market_relevance FROM article_analysis x "
            " WHERE x.article_id=a.id AND x.status IN ('ok','success') "
            " ORDER BY x.updated_at DESC LIMIT 1) AS market_relevance "
            "FROM articles a"
        )
        where: list[str] = []
        params: list = []
        if source_id:
            where.append("a.source_id=?")
            params.append(source_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(a.published_at, a.discovered_at) DESC LIMIT ?"
        params.append(limit)
        with self._conn:
            rows = self._conn.execute(sql, params).fetchall()
        return list(rows)

    def get_analysis_for_article(self, article_id: int) -> Optional[sqlite3.Row]:
        """获取指定文章的 AI 分析记录（优先成功分析）。

        用于 News Archive 单篇页展示完整 AI 详情（摘要/关键观点/主题/实体/相关性）。
        若没有成功分析，返回失败记录（如果有）。无记录返回 None。
        """
        sql = (
            "SELECT * FROM article_analysis WHERE article_id=? "
            "ORDER BY CASE status WHEN 'success' THEN 0 WHEN 'ok' THEN 0 ELSE 1 END, "
            "updated_at DESC LIMIT 1"
        )
        with self._conn:
            row = self._conn.execute(sql, (article_id,)).fetchone()
        return row if row else None

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
