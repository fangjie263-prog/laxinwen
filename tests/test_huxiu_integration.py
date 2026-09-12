"""正式 Huxiu source wiring tests using the checked-in real HTML fixtures."""

import json
from pathlib import Path

from news.config import load_site_config, resolve_config_source
from news.discover import DiscoveredItem
from news.gui import _ALL_SOURCE_IDS, _SOURCE_OPTIONS
from news.model import Article
from news.scheduler_config import SUPPORTED_SOURCES
from news.sources import get_adapter


ROOT = Path(__file__).resolve().parents[1]


class FixtureFetcher:
    def __init__(self, channel_html: str, article_html: str):
        self.channel_html = channel_html
        self.article_html = article_html
        self.calls: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        return self.channel_html if "/article/" in url and not url.endswith(".html") else self.article_html


class CursorFetcher(FixtureFetcher):
    def __init__(self, channel_html: str, article_html: str, pages: dict[str, list[dict]], cursors: dict[str, str]):
        super().__init__(channel_html, article_html)
        self.pages = pages
        self.cursors = cursors
        self.post_calls: list[dict] = []

    def post_form(self, url: str, data: dict, **kwargs) -> str:
        self.post_calls.append(dict(data))
        cursor = str(data["last_id"])
        return json.dumps(
            {
                "success": True,
                "data": {
                    "datalist": self.pages.get(cursor, []),
                    "last_id": self.cursors.get(cursor, ""),
                },
            }
        )


def _api_rows(start: int, count: int = 12) -> list[dict]:
    return [
        {
            "aid": start - index,
            "title": f"cursor article {start - index}",
            "url": f"https://www.huxiu.com/article/{start - index}.html",
            "dateline": 1789000000 - index,
            "pic_path": f"https://img.huxiucdn.com/{start - index}.jpg",
            "user_info": {"username": "cursor author"},
        }
        for index in range(count)
    ]


def test_huxiu_is_registered_across_config_gui_and_scheduler():
    cfg = load_site_config("huxiu", ROOT / "sites")
    adapter = get_adapter(cfg)

    assert cfg["adapter"] == "huxiu"
    assert resolve_config_source(cfg) == "list:https://www.huxiu.com/article/"
    assert adapter is not None
    assert "huxiu" in _ALL_SOURCE_IDS
    assert "huxiu" in tuple(source_id for source_id, _ in _SOURCE_OPTIONS)
    assert "huxiu" in SUPPORTED_SOURCES


def test_huxiu_pipeline_adapter_contract_uses_real_fixtures():
    cfg = load_site_config("huxiu", ROOT / "sites")
    adapter = get_adapter(cfg)
    channel = (ROOT / "tests" / "fixtures" / "huxiu" / "channel.html").read_text(encoding="utf-8")
    article_html = (ROOT / "tests" / "fixtures" / "huxiu" / "002.html").read_text(encoding="utf-8")
    fetcher = FixtureFetcher(channel, article_html)

    items = adapter.discover(fetcher=fetcher, max_items=5, existing_urls=set())

    assert items
    assert all(isinstance(item, DiscoveredItem) for item in items)
    assert len({item.url for item in items}) == len(items)

    article = Article(
        source_id="huxiu",
        source_name=cfg["name"],
        canonical_url=items[0].url,
        title=items[0].title or "placeholder",
        language=cfg["language"],
    )
    assert adapter.extract_article(article, article_html, url="https://www.huxiu.com/article/4890724.html")
    assert article.title
    assert len(article.body_text) > 2_000
    assert article.body_html
    assert article.images
    assert article.language == "zh-CN"


def test_huxiu_cursor_discovery_expands_real_channel_window_and_deduplicates():
    cfg = load_site_config("huxiu", ROOT / "sites")
    adapter = get_adapter(cfg)
    channel = (ROOT / "tests" / "fixtures" / "huxiu" / "channel.html").read_text(encoding="utf-8")
    article = (ROOT / "tests" / "fixtures" / "huxiu" / "002.html").read_text(encoding="utf-8")
    fetcher = CursorFetcher(
        channel,
        article,
        pages={"0": _api_rows(4890725), "1": _api_rows(4890699), "2": []},
        cursors={"0": "1", "1": "2", "2": ""},
    )

    items = adapter.discover(fetcher=fetcher, max_items=20, existing_urls=set())

    assert len(items) == 20
    assert len({item.url for item in items}) == 20
    assert len(fetcher.post_calls) == 2
    assert fetcher.post_calls[0]["last_id"] == "0"
    assert fetcher.post_calls[0]["pagesize"] == 12
    assert all(item.url.startswith("https://www.huxiu.com/article/") for item in items)


def test_huxiu_cursor_discovery_stops_on_repeated_cursor_and_large_limit_is_bounded():
    cfg = load_site_config("huxiu", ROOT / "sites")
    adapter = get_adapter(cfg)
    channel = (ROOT / "tests" / "fixtures" / "huxiu" / "channel.html").read_text(encoding="utf-8")
    article = (ROOT / "tests" / "fixtures" / "huxiu" / "002.html").read_text(encoding="utf-8")
    fetcher = CursorFetcher(
        channel,
        article,
        pages={"0": _api_rows(4890725)},
        cursors={"0": "0"},
    )
    adapter.api_max_pages = 2

    items = adapter.discover(fetcher=fetcher, max_items=100, existing_urls=set())

    assert len(items) <= 24
    assert len(fetcher.post_calls) == 1
