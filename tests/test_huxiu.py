"""Real-HTML tests for the independent Huxiu prototype.

Fixtures are captured from huxiu.com on 2026-09-12; they are not synthetic
HTML.  The tests intentionally assert content quality and pollution boundaries.
"""

from pathlib import Path

from news.model import Article
from news.sources.huxiu import (
    HuxiuAdapter,
    extract_huxiu_article,
    normalize_huxiu_url,
    parse_channel_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "huxiu"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _result(name: str):
    article_id = Path(name).stem
    return extract_huxiu_article(_html(name), url=f"https://www.huxiu.com/article/{article_id}.html")


def test_discovery_returns_real_article_cards_and_deduplicates():
    items = parse_channel_page(_html("channel.html"))
    assert len(items) == 12
    assert len({item.url for item in items}) == len(items)
    assert all("/article/" in item.url and item.title for item in items)


def test_url_normalization_merges_desktop_mobile_and_tracking_variants():
    expected = "https://www.huxiu.com/article/4890703.html"
    assert normalize_huxiu_url("https://m.huxiu.com/article/4890703.html") == expected
    assert normalize_huxiu_url(expected + "?utm_source=rss#top") == expected


def test_title_authors_and_published_time_from_real_page():
    result = _result("001.html")
    assert result.title == "智谱上市后募资，大模型战场变了"
    assert result.authors == ["宋思杭"]
    assert result.published_at is not None
    assert result.published_at.tzinfo is not None
    assert result.published_at.isoformat() == "2026-09-12T13:00:39+00:00"


def test_body_is_substantial_and_not_navigation_or_footer():
    result = _result("001.html")
    assert len(result.body_text) > 1000
    assert "导航菜单" not in result.body_text
    assert "Copyright" not in result.body_text
    assert "下载虎嗅APP" not in result.body_text
    assert "本内容未经允许不得转载" not in result.body_text


def test_body_has_no_related_or_comments_contamination():
    result = _result("002.html")
    assert len(result.body_text) > 3000
    assert result.metadata["blocks"]
    assert "相关推荐" not in result.body_text
    assert "热门文章" not in result.body_text


def test_images_are_absolute_and_inline_order_is_retained():
    result = _result("002.html")
    assert result.images
    assert all(image.startswith("https://") for image in result.images)
    kinds = [block["type"] for block in result.blocks]
    assert "image" in kinds
    first_image = kinds.index("image")
    assert first_image > 0
    assert any(block["type"] == "caption" for block in result.blocks)


def test_metadata_section_tags_and_original_source_are_non_schema_fields():
    result = _result("005.html")
    assert result.section
    assert result.metadata["section"] == result.section
    assert "embedded_tag_data_present" in result.metadata
    # The current page's devalue state exposes tag_name entries; mapping them
    # to names needs a devalue decoder and is intentionally metadata-only.
    assert result.metadata["embedded_tag_data_present"] is True


def test_article_mapping_matches_existing_model_without_schema_changes():
    result = _result("001.html")
    article = result.to_article()
    assert isinstance(article, Article)
    assert article.source_id == "huxiu"
    assert article.source_name == "虎嗅"
    assert article.canonical_url == "https://www.huxiu.com/article/4890703.html"
    assert article.body_text == result.body_text
    assert article.images == result.images
    assert article.language == "zh-CN"


def test_adapter_extracts_into_existing_article_contract():
    result = _result("003.html")
    article = Article(source_id="huxiu", source_name="虎嗅", canonical_url=result.canonical_url, title="")
    adapter = HuxiuAdapter("huxiu", "虎嗅")
    assert adapter.extract_article(article, _html("003.html"), result.canonical_url)
    assert article.title == result.title
    assert len(article.body_text) > 1000
