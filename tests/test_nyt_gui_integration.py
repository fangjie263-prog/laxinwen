"""Small integration checks for NYT source registration and shared Reader output."""

from datetime import datetime, timezone
from pathlib import Path

from news.config import load_site_config
from news.gui import _ALL_SOURCE_IDS, _SOURCE_OPTIONS
from news.model import Article
from news.news_archive import render_article_section
from news.sources import get_adapter


ROOT = Path(__file__).resolve().parents[1]


def test_nytchinese_is_registered_and_maps_to_shared_article_fields():
    cfg = load_site_config("nytchinese", ROOT / "sites")
    adapter = get_adapter(cfg)
    article = Article(
        source_id="nytchinese",
        source_name=cfg["name"],
        canonical_url="https://cn.nytimes.com/technology/20260904/example/",
        title="placeholder",
    )
    html = (ROOT / "tests" / "fixtures" / "nytchinese" / "technology.html").read_text(
        encoding="utf-8"
    )

    assert adapter is not None
    assert adapter.extract_article(article, html) is True
    assert article.title
    assert article.published_at is not None
    assert article.body_text
    assert article.body_html and "<img" in article.body_html
    assert article.images
    assert article.canonical_url.startswith("https://cn.nytimes.com/")


def test_nytchinese_is_available_in_gui_and_shared_reader():
    source_ids = tuple(source_id for source_id, _ in _SOURCE_OPTIONS)
    assert "nytchinese" in source_ids
    assert "nytchinese" in _ALL_SOURCE_IDS

    html = render_article_section(
        index=1,
        article_id=2269,
        title="纽约时报中文标题",
        source_name="NYT 纽约时报中文网",
        authors=["作者甲"],
        published_at=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        canonical_url="https://cn.nytimes.com/culture/20260904/example/",
        body_text="中文正文",
        body_html='<p>中文正文</p><img src="https://example.com/photo.jpg">',
        ai_status="none",
        analysis={},
    )
    for expected in (
        "纽约时报中文标题",
        "作者甲",
        "中文正文",
        "https://example.com/photo.jpg",
        "https://cn.nytimes.com/culture/20260904/example/",
    ):
        assert expected in html
