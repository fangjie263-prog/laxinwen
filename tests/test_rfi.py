"""RFI（法广中文）Source Adapter 测试。

覆盖 Phase 2 验收项：

1. RfiAdapter 注册与发现调用链（load_site_config → get_adapter → RfiAdapter.discover）
2. 官方 RSS → RSSHub 回退（官方 RSS 失败时 RSSHub 返回文章）
3. RSSHub 只给 summary（content_html 为空）→ has_usable_content=False
4. HTML fallback：RSS 只有 summary → pipeline fetch 原文 → .t-content__chapo +
   .t-content__body → body_html / body_text
5. 完整正文短路：RSS content:encoded → content_html → has_usable_content=True →
   fetcher.calls == []（0 fetch）→ body_html != "" / body_text != ""
6. 完整正文短路作为公共 pipeline regression test
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.config import load_site_config  # noqa: E402
from news.discover import (  # noqa: E402
    DiscoveredItem,
    discover_from_rss,
    discover_for_site,
    has_usable_content,
)
from news.fetch import BaseFetcher  # noqa: E402
from news.pipeline import Pipeline  # noqa: E402
from news.sources import get_adapter  # noqa: E402
from news.sources.rfi import (  # noqa: E402
    RfiAdapter,
    extract_body_from_html,
    extract_title,
)
from news.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------------
# 测试 fixture 辅助
# ---------------------------------------------------------------------------


class FakeFetcher(BaseFetcher):
    """离线假抓取器：记录 calls，按 URL 返回对应内容。"""

    def __init__(self, rss: str, html: str = ""):
        self.rss = rss
        self.html = html
        self.calls: list[str] = []
        self.fail_urls: set[str] = set()

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        # 模拟官方 RSS 站点不可达
        if url in self.fail_urls:
            raise RuntimeError(f"connection refused: {url}")
        if "rss" in url or "rsshub" in url or "feed" in url:
            return self.rss
        return self.html

    def close(self) -> None:
        pass


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "rfi.db")
    yield s
    s.close()


# RFI 完整正文 RSS（官方 RSS content:encoded 场景）—— 正文超过阈值，应短路
_FULL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>法广 - RFI</title>
<item>
<title>法国各地热浪考验城市居民</title>
<link>https://www.rfi.fr/cn/france/20260815-heatwave</link>
<description>导语：法国正经历最新热浪。</description>
<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"><![CDATA[
<p class="t-content__chapo">导语：法国正经历最新热浪，城市居民苦不堪言。</p>
<div class="t-content__body">
<p>这是正文第一段。法国多个城市的气温连续多日超过 40 摄氏度，地方政府正在部署应急措施，为居民提供避暑中心，并延长公共泳池的开放时间。</p>
<p>这是正文第二段。气象部门预计高温将持续到下周末，专家呼吁居民减少户外活动，并关注老年人等脆弱人群的健康状况。</p>
<p>这是正文第三段。多个城市已经开始在公园和广场安装临时喷雾装置，并鼓励商场开放空调区域供市民纳凉。卫生部门也发布了防暑指南，提醒民众及时补充水分。</p>
</div>
]]></content:encoded>
<pubDate>Sat, 15 Aug 2026 10:43:37 GMT</pubDate>
</item>
</channel></rss>
"""

# RSSHub 场景：只给 summary（导语），无 content:encoded → content_html 为空
_SUMMARY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>法广 - 时事与新闻直播 - RFI</title>
<item>
<title>高市致辞不谈反省 四阁僚参拜靖国神社</title>
<link>https://www.rfi.fr/cn/politics/20260815-yasukuni</link>
<description>&lt;p class=&quot;t-content__chapo&quot;&gt;
日本高市在战败日致辞中未提反省，四名内阁成员参拜靖国神社。
&lt;/p&gt;</description>
<pubDate>Sat, 15 Aug 2026 11:41:07 GMT</pubDate>
</item>
</channel></rss>
"""

# RFI 原文 HTML（HTML fallback 场景）—— 含 .t-content__chapo + .t-content__body
_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>高市致辞不谈反省 四阁僚参拜靖国神社 - RFI</title>
<meta property="og:title" content="高市致辞不谈反省 四阁僚参拜靖国神社"/>
</head>
<body>
<article>
<h1>高市致辞不谈反省 四阁僚参拜靖国神社</h1>
<p class="t-content__chapo">日本高市在战败日致辞中未提反省，四名内阁成员参拜靖国神社。</p>
<div class="t-content__body">
<p>这是从原文页面提取的正文第一段。</p>
<p>这是从原文页面提取的正文第二段，包含了完整的报道内容。</p>
<p>这是从原文页面提取的正文第三段。</p>
</div>
</article>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 1. RfiAdapter 注册与发现调用链
# ---------------------------------------------------------------------------


class TestRfiRegistration:
    def test_get_adapter_returns_rfi_adapter(self):
        """load_site_config("rfi") → get_adapter(...) → RfiAdapter。"""
        cfg = load_site_config("rfi")
        assert cfg.get("adapter") == "rfi"
        adapter = get_adapter(cfg)
        assert isinstance(adapter, RfiAdapter)
        assert adapter.source_id == "rfi"

    def test_discover_for_site_dispatch_to_adapter(self):
        """discover_for_site 对 rfi 站点走 adapter 而非通用 discover。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)
        items = discover_for_site(cfg, fetcher=fetcher, max_items=5)
        # RSSHub 路径返回 summary 条目
        assert len(items) >= 1
        # 只请求了 RSS/RSSHub，没有走列表页/load-more
        assert all("rss" in u or "rsshub" in u for u in fetcher.calls)


# ---------------------------------------------------------------------------
# 2. 官方 RSS → RSSHub 回退
# ---------------------------------------------------------------------------


class TestRfiDiscoveryFallback:
    def test_official_rss_fails_then_rsshub(self):
        """官方 RSS 不可达时回退到 RSSHub。"""
        fetcher = FakeFetcher(_SUMMARY_RSS)
        # 模拟官方 RSS 站点 www.rfi.fr 不可达
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = RfiAdapter("rfi", "RFI")
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert len(items) >= 1
        assert items[0].title == "高市致辞不谈反省 四阁僚参拜靖国神社"
        # 确认先尝试官方 RSS，失败后走 RSSHub
        assert fetcher.calls[0] == "https://www.rfi.fr/zh/rss"
        assert any("rsshub" in u for u in fetcher.calls)

    def test_rsshub_summary_has_no_content_html(self):
        """RSSHub 只给 summary，content_html 为空 → has_usable_content=False。"""
        fetcher = FakeFetcher(_SUMMARY_RSS)
        adapter = RfiAdapter("rfi", "RFI")
        items = adapter.discover(fetcher=fetcher, max_items=5)
        item = items[0]
        assert item.summary  # 有导语
        assert not item.content_html  # 无完整正文
        assert has_usable_content(item.content_html) is False


# ---------------------------------------------------------------------------
# 3. has_usable_content 语义
# ---------------------------------------------------------------------------


class TestHasUsableContent:
    def test_none_or_empty_is_false(self):
        assert has_usable_content(None) is False
        assert has_usable_content("") is False

    def test_short_summary_is_false(self):
        """content_html 只有非常短的摘要 → False。"""
        assert has_usable_content("<p>只有一句很短的话。</p>") is False

    def test_full_content_is_true(self):
        """content_html 有足量正文 → True。"""
        from news.discover import discover_from_rss

        items = discover_from_rss(_FULL_RSS)
        assert items and items[0].content_html
        assert has_usable_content(items[0].content_html) is True


# ---------------------------------------------------------------------------
# 4. HTML fallback（RSS 只有 summary → fetch 原文 → 提取正文）
# ---------------------------------------------------------------------------


class TestHtmlFallback:
    def test_extract_body_from_html(self):
        """.t-content__chapo + .t-content__body 提取 → body_html / body_text。"""
        body_html, body_text = extract_body_from_html(_ARTICLE_HTML)
        assert "t-content__chapo" in body_html
        assert "t-content__body" in body_html
        assert "这是从原文页面提取的正文第一段" in body_text
        assert "这是从原文页面提取的正文第三段" in body_text

    def test_extract_title_fallback(self):
        """标题提取：h1 → og:title → title。"""
        assert extract_title(_ARTICLE_HTML) == "高市致辞不谈反省 四阁僚参拜靖国神社"

    def test_pipeline_html_fallback_through_adapter(self, storage):
        """RSS 只有 summary → has_usable_content=False → pipeline fetch 原文 →
        adapter.extract_article 用 .t-content__chapo + .t-content__body 提取。"""
        fetcher = FakeFetcher(_SUMMARY_RSS, html=_ARTICLE_HTML)
        adapter = RfiAdapter("rfi", "RFI")
        pipe = Pipeline(storage, fetcher=fetcher, max_items=5)
        from news.discover import discover_from_rss

        items = discover_from_rss(_SUMMARY_RSS)
        # 走完整调用链：_ingest_items(adapter=...) → fetch → adapter.extract_article
        stats = pipe._ingest_items(
            items, "rfi", "RFI", "zh", {}, [], adapter=adapter
        )
        assert stats.fetched_ok == 1
        assert stats.extracted_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_html and "t-content__body" in art.body_html
        assert art.body_text and "这是从原文页面提取的正文第一段" in art.body_text
        pipe.close()

    def test_pipeline_html_fallback_full_run_site(self, storage):
        """run_site("rfi") 走完整调用链：RfiAdapter → discover → HTML fallback。"""
        fetcher = FakeFetcher(_SUMMARY_RSS, html=_ARTICLE_HTML)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        stats = pipe.run_site("rfi")
        assert stats.discovered >= 1
        # 下载原文成功 → 提取成功
        assert stats.fetched_ok >= 1
        assert stats.extracted_ok >= 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "正文" in art.body_text
        pipe.close()


# ---------------------------------------------------------------------------
# 5. 完整正文短路（content:encoded → content_html → 0 fetch）
# ---------------------------------------------------------------------------


class TestContentShortCircuit:
    def test_short_circuit_zero_fetch(self, storage):
        """RSS content:encoded → content_html → has_usable_content=True →
        fetcher.calls == []（0 fetch）→ body_html != "" / body_text != ""。"""
        fetcher = FakeFetcher(_FULL_RSS)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        from news.discover import discover_from_rss

        items = discover_from_rss(_FULL_RSS)
        assert items and items[0].content_html
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 短路：不发任何额外请求
        assert fetcher.calls == []
        assert stats.fetched_ok == 1
        assert stats.extracted_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_html != ""
        assert art.body_text != ""
        assert "正文第一段" in art.body_text
        pipe.close()


# ---------------------------------------------------------------------------
# 6. 完整正文短路作为公共 pipeline regression test（通用站点也受益）
# ---------------------------------------------------------------------------


class TestPipelineContentShortCircuitRegression:
    """无论是否 adapter 站点，只要 RSS 带完整 content_html 就应短路。"""

    def test_generic_pipeline_short_circuit(self, storage):
        """通用站点（无 adapter）也可用 content_html 短路，0 fetch。"""
        fetcher = FakeFetcher(_FULL_RSS)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        from news.discover import discover_from_rss

        items = discover_from_rss(_FULL_RSS)
        # 无 adapter 的通用路径
        stats = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        assert fetcher.calls == []
        assert stats.fetched_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_html != ""
        assert art.body_text != ""
        pipe.close()

    def test_short_summary_still_fetches(self, storage):
        """content_html 为空/很短时仍走 fetch + extract，不短路。"""
        fetcher = FakeFetcher(_FULL_RSS, html=_ARTICLE_HTML)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        from news.discover import DiscoveredItem

        # 一个只有导语（content_html 很短）的条目 → 必须 fetch
        item = DiscoveredItem(
            url="https://www.rfi.fr/cn/france/20260815-heatwave",
            title="测试标题",
            content_html="<p>这是一句很短的导语，不构成完整正文。</p>",
        )
        stats = pipe._ingest_items([item], "rfi", "RFI", "zh", {}, [])
        # 触发 fetch + extract
        assert fetcher.calls == ["https://www.rfi.fr/cn/france/20260815-heatwave"]
        assert stats.fetched_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "正文" in art.body_text
        pipe.close()
