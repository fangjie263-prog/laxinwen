"""正式 Huxiu source wiring tests using the checked-in real HTML fixtures."""

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
