"""Tests for exporters (JSONL + Markdown)."""

from datetime import datetime, timezone

from laxinwen.exporters import article_to_markdown, export_jsonl, export_markdown
from laxinwen.model import Article, utcnow


def _article(url: str, title: str, published: datetime) -> Article:
    return Article(
        source_id="eco",
        source_name="ECO",
        canonical_url=url,
        title=title,
        authors=["Lusa", "Redação"],
        published_at=published,
        body_text="Corpo da notícia.\n\nSegundo parágrafo.",
        status="ok",
        discovered_at=utcnow(),
    )


def test_article_to_markdown():
    a = _article(
        "https://eco.sapo.pt/2026/08/08/titulo/",
        "Um título",
        datetime(2026, 8, 8, 22, 23, 34, tzinfo=timezone.utc),
    )
    md = article_to_markdown(a)
    assert md.startswith("---")
    assert "title: Um título" in md
    assert "author: Lusa, Redação" in md
    assert "url: https://eco.sapo.pt/2026/08/08/titulo/" in md
    assert "Corpo da notícia." in md


def test_export_jsonl(tmp_path):
    out = tmp_path / "articles.jsonl"
    a = _article(
        "https://eco.sapo.pt/2026/08/08/x/",
        "X",
        datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    path = export_jsonl([a], out)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json

    data = json.loads(lines[0])
    assert data["title"] == "X"
    assert data["canonical_url"] == "https://eco.sapo.pt/2026/08/08/x/"


def test_export_markdown_organizes_by_year_month(tmp_path):
    a = _article(
        "https://eco.sapo.pt/2026/08/08/x/",
        "X",
        datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    written = export_markdown([a], tmp_path / "md")
    assert len(written) == 1
    assert written[0].parent.name == "08"
    assert written[0].parent.parent.name == "2026"
    content = written[0].read_text(encoding="utf-8")
    assert "title: X" in content
