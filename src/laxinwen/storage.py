"""SQLite storage — the single source of truth for the first version."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .model import Article
from .normalize import canonicalize_url, title_fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    canonical_url   TEXT NOT NULL UNIQUE,
    title           TEXT,
    title_fp        TEXT,
    authors         TEXT,
    published_at    TEXT,
    discovered_at   TEXT,
    fetched_at      TEXT,
    body_text       TEXT,
    body_html       TEXT,
    images          TEXT,
    lead_image      TEXT,
    language        TEXT,
    status          TEXT,
    errors          TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_title_fp ON articles(title_fp);
"""


def _to_json_list(values: list[str]) -> str:
    import json

    return json.dumps(values, ensure_ascii=False)


def _from_json_list(text: str | None) -> list[str]:
    import json

    if not text:
        return []
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def insert(self, article: Article, *, strip_site_suffix: str | None = None) -> bool:
        """Insert an article. Returns True on insert, False on duplicate.

        Dedup happens at the DB level via the canonical_url UNIQUE constraint;
        title fingerprints are also recorded for reporting.
        """
        canon = canonicalize_url(article.canonical_url)
        fp = title_fingerprint(article.title, strip_site_suffix=strip_site_suffix)
        try:
            cur = self.conn.execute(
                """
                INSERT INTO articles (
                    source_id, source_name, canonical_url, title, title_fp,
                    authors, published_at, discovered_at, fetched_at,
                    body_text, body_html, images, lead_image, language, status, errors
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    article.source_id,
                    article.source_name,
                    canon,
                    article.title,
                    fp or None,
                    _to_json_list(article.authors),
                    article.published_at_iso(),
                    article.discovered_at.isoformat(),
                    article.fetched_at.isoformat() if article.fetched_at else None,
                    article.body_text,
                    article.body_html,
                    _to_json_list(article.images),
                    article.lead_image,
                    article.language,
                    article.status,
                    _to_json_list(article.errors),
                ),
            )
            self.conn.commit()
            article.id = cur.lastrowid
            return True
        except sqlite3.IntegrityError:
            return False

    def get_by_url(self, canonical_url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM articles WHERE canonical_url = ?",
            (canonicalize_url(canonical_url),),
        ).fetchone()

    def count(self, *, source_id: str | None = None) -> int:
        if source_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM articles WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    def list_articles(self, *, limit: int = 50, source_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM articles"
        args: list = []
        if source_id:
            q += " WHERE source_id = ?"
            args.append(source_id)
        q += " ORDER BY COALESCE(published_at, discovered_at) DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["authors"] = _from_json_list(d.get("authors"))
            d["images"] = _from_json_list(d.get("images"))
            d["errors"] = _from_json_list(d.get("errors"))
            out.append(d)
        return out

    def status(self) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error
            FROM articles
            """
        ).fetchone()
        by_source = {
            r["source_id"]: r["total"]
            for r in self.conn.execute(
                "SELECT source_id, COUNT(*) total FROM articles GROUP BY source_id"
            ).fetchall()
        }
        return {
            "db_path": str(self.db_path),
            "total": row["total"] or 0,
            "ok": row["ok"] or 0,
            "error": row["error"] or 0,
            "by_source": by_source,
        }
