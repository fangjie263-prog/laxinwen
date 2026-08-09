"""Tests for SQLite storage: insert, unique constraint, dedupe, status."""

import sqlite3

from laxinwen.model import Article, utcnow
from laxinwen.storage import Storage


def _make_article(url: str, title: str = "Some headline", **kw) -> Article:
    return Article(
        source_id="eco",
        source_name="ECO",
        canonical_url=url,
        title=title,
        body_text="Body text here.",
        status="ok",
        discovered_at=utcnow(),
        **kw,
    )


def test_insert_and_count(tmp_path):
    db = tmp_path / "test.db"
    with Storage(db) as s:
        a = _make_article("https://eco.sapo.pt/2026/08/08/x/")
        assert s.insert(a) is True
        assert s.count() == 1
        assert s.count(source_id="eco") == 1


def test_unique_constraint_on_canonical_url(tmp_path):
    db = tmp_path / "test.db"
    with Storage(db) as s:
        assert s.insert(_make_article("https://eco.sapo.pt/x/")) is True
        # same URL with tracking params / fragment → duplicate
        dup = _make_article(
            "https://eco.sapo.pt/x/?utm_source=rss&fbclid=zzz#top"
        )
        assert s.insert(dup) is False
        assert s.count() == 1


def test_get_by_url(tmp_path):
    db = tmp_path / "test.db"
    with Storage(db) as s:
        s.insert(_make_article("https://eco.sapo.pt/x/"))
        row = s.get_by_url("https://eco.sapo.pt/x/?utm_source=feed")
        assert row is not None
        assert row["canonical_url"] == "https://eco.sapo.pt/x/"


def test_duplicate_urls_are_not_reinserted(tmp_path):
    db = tmp_path / "test.db"
    with Storage(db) as s:
        for _ in range(3):
            assert s.insert(_make_article("https://eco.sapo.pt/x/")) is (True if _ == 0 else False)
        assert s.count() == 1


def test_status_counts(tmp_path):
    db = tmp_path / "test.db"
    with Storage(db) as s:
        s.insert(_make_article("https://eco.sapo.pt/a/", title="A"))
        bad = _make_article("https://eco.sapo.pt/b/", title="B")
        bad.status = "error"
        s.insert(bad)
        st = s.status()
        assert st["total"] == 2
        assert st["ok"] == 1
        assert st["error"] == 1
        assert st["by_source"]["eco"] == 2
