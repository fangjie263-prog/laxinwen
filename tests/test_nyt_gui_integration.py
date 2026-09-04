"""Small integration checks for NYT source registration and shared Reader output."""

from datetime import datetime, timezone
from pathlib import Path

from news.config import load_site_config
from news.discover import DiscoveredItem
from news.gui import _ALL_SOURCE_IDS, _SOURCE_OPTIONS
from news.model import Article
from news.news_archive import render_article_section
from news.pipeline import Pipeline
from news.storage import Storage
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


def test_nyt_rss_summary_is_never_promoted_to_article_body(monkeypatch):
    import news.sources.nytchinese as nyt_source

    summary = "<p>这是 RSS 摘要，不是文章全文。</p>" * 4
    monkeypatch.setattr(
        nyt_source,
        "discover_from_rss",
        lambda *_args, **_kwargs: [
            DiscoveredItem(
                url="https://cn.nytimes.com/technology/20260904/example/",
                title="摘要测试",
                content_html=summary,
            )
        ],
    )

    class Fetcher:
        def fetch(self, _url):
            return ""

    adapter = get_adapter({
        "id": "nytchinese",
        "name": "NYT 纽约时报中文网",
        "adapter": "nytchinese",
        "rss": "https://cn.nytimes.com/rss/news.xml",
        "allow_summary_as_content": False,
    })
    items = adapter.discover(fetcher=Fetcher(), max_items=100)
    assert len(items) == 1
    assert items[0].content_html is None


def test_nyt_article_html_is_fetched_and_parsed_after_rss_discovery(tmp_path):
    fixture = (ROOT / "tests" / "fixtures" / "nytchinese" / "technology.html").read_text(
        encoding="utf-8"
    )

    class Fetcher:
        def __init__(self):
            self.article_calls = []

        def fetch_article(self, url):
            self.article_calls.append(url)
            return fixture

        def close(self):
            pass

    storage = Storage(tmp_path / "pipeline.db")
    fetcher = Fetcher()
    try:
        adapter = get_adapter({
            "id": "nytchinese",
            "name": "NYT 纽约时报中文网",
            "adapter": "nytchinese",
            "rss": "https://cn.nytimes.com/rss/news.xml",
            "allow_summary_as_content": False,
        })
        summary = "<p>这是 RSS 摘要，不是文章全文。</p>" * 4
        stats = Pipeline(storage, fetcher=fetcher, max_items=1)._ingest_items(
            [DiscoveredItem(
                url="https://cn.nytimes.com/technology/20260904/openai-hugging-face-hacking/",
                title="摘要标题",
                content_html=None,
                summary=summary,
            )],
            "nytchinese",
            "NYT 纽约时报中文网",
            "zh-CN",
            {},
            [],
            adapter=adapter,
            target_usable=1,
        )
        article = storage.list_articles(source_id="nytchinese", limit=1)[0]
        assert stats.usable == 1
        assert len(fetcher.article_calls) == 1
        assert len(article.body_text) > 200
        assert "这是 RSS 摘要" not in article.body_text
    finally:
        storage.close()


def test_nyt_section_discovery_deduplicates_existing_and_limits(monkeypatch):
    import news.sources.nytchinese as nyt_source

    section_html = (ROOT / "nyt-recon" / "sections" / "001-国际-desktop.html").read_text(
        encoding="utf-8"
    )
    monkeypatch.setattr(nyt_source, "discover_from_rss", lambda *_a, **_k: [])

    class Fetcher:
        def fetch(self, _url):
            return section_html

    section = {"url": "https://cn.nytimes.com/world/"}
    adapter = get_adapter({
        "id": "nytchinese",
        "name": "NYT 纽约时报中文网",
        "adapter": "nytchinese",
        "rss": "https://cn.nytimes.com/rss/news.xml",
        "sections": [section, section],
        "allow_summary_as_content": False,
    })
    all_items = adapter.discover(fetcher=Fetcher(), max_items=100)
    existing = {all_items[0].url}
    assert len(all_items) >= 10
    assert len({item.url for item in all_items}) == len(all_items)
    filtered = adapter.discover(fetcher=Fetcher(), max_items=100, existing_urls=existing)
    assert len(filtered) == len(all_items) - 1
    assert all_items[0].published_at >= all_items[-1].published_at


def test_nyt_refresh_updates_existing_article_without_duplicate(tmp_path):
    fixture = (ROOT / "tests" / "fixtures" / "nytchinese" / "technology.html").read_text(
        encoding="utf-8"
    )

    class Fetcher:
        def __init__(self):
            self.calls = []

        def fetch_article(self, url):
            self.calls.append(url)
            return fixture

        def close(self):
            pass

    storage = Storage(tmp_path / "refresh.db")
    fetcher = Fetcher()
    try:
        article = Article(
            source_id="nytchinese",
            source_name="NYT 纽约时报中文网",
            canonical_url="https://cn.nytimes.com/technology/20260904/openai-hugging-face-hacking/",
            title="旧摘要",
            body_text="旧 RSS summary",
            body_html="<p>旧 RSS summary</p>",
            status="fetched",
        )
        article_id, inserted = storage.insert_article(article)
        assert inserted
        stats = Pipeline(storage, fetcher=fetcher, max_items=100).refresh_source(
            "nytchinese", limit=100
        )
        refreshed = storage.get_article(article_id)
        assert stats.usable == 1
        assert len(fetcher.calls) == 1
        assert refreshed is not None
        assert refreshed.id == article_id
        assert len(refreshed.body_text) > 200
        assert storage.count(source_id="nytchinese") == 1
    finally:
        storage.close()
