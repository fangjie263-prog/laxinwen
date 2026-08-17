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
from news.normalize import canonicalize_url  # noqa: E402
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
    """离线假抓取器：记录 calls，按 URL 返回对应内容。

    - ``rss`` / ``html``：兜底内容（任何含 rss/rsshub/feed 的 URL 返回 ``rss``，
      其余返回 ``html``）；
    - ``url_map``：可选，精确 URL → 内容，优先于兜底（用于分类聚合测试，不同
      分类返回不同 feed）；
    - ``fail_urls`` / ``fail_substrings``：精确 URL / 包含子串即抛连接错误。
    """

    def __init__(
        self,
        rss: str = "",
        html: str = "",
        url_map: dict[str, str] | None = None,
    ):
        self.rss = rss
        self.html = html
        self.url_map = url_map or {}
        self.calls: list[str] = []
        self.fail_urls: set[str] = set()
        self.fail_substrings: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        # 模拟站点不可达（精确 URL 或子串匹配）
        if url in self.fail_urls:
            raise RuntimeError(f"connection refused: {url}")
        if any(sub in url for sub in self.fail_substrings):
            raise RuntimeError(f"connection refused: {url}")
        if url in self.url_map:
            return self.url_map[url]
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


def _mk_rss(items: list[tuple[str, str]]) -> str:
    """由 ``(title, link)`` 列表构造一个最小 RSS feed（用于分类聚合测试）。"""
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>Sat, 15 Aug 2026 10:00:00 GMT</pubDate></item>"
        for title, link in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>法广 - RFI</title>'
        + entries
        + "</channel></rss>"
    )


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


# ---------------------------------------------------------------------------
# Phase 4：content 缺失但 summary 携带完整 HTML 正文 → 直接用 summary 作
# content_html → 0-fetch 短路；figure/figcaption 图片区不进入 body_text。
# ---------------------------------------------------------------------------

# RFI RSSHub 实际返回：无 content:encoded，但 summary 携带完整正文 HTML
# （.t-content__chapo 导语 + .t-content__main-media 图片区 + 多段正文）。
_SUMMARY_FULL_BODY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>法广 - 时事与新闻直播 - RFI</title>
<item>
<title>仅靠对华“去风险”已经不够了</title>
<link>https://www.rfi.fr/cn/germany/20260815-derisking</link>
<description><![CDATA[
<p class="t-content__chapo">据德国《商报》报道，在对华事务上，问题已不再只涉及贸易和投资前景，而早已涉及就业岗位的流失以及政治行动能力。</p>
<div class="t-content__main-media">
<figure class="m-item-image m-item-image--16x9 m-item-image--has-caption">
<img alt="德国汽车制造商采用中国零部件。" class="a-img" height="576" src="https://s.rfi.fr/media/display/x.jpg" width="1024"/>
<figcaption class="a-figcaption">德国大众（Volkswagen）、宝马（BMW）和梅赛德斯-奔驰（Mercedes-Benz）等德国汽车制造商也越来越多地采用中国零部件。REUTERS - Benoit Tessier</figcaption>
</figure>
</div>
<p>德国汽车工业在7月底宣布大规模裁员：大众汽车最多可能削减10万个工作岗位，保时捷削减9000个，宝马削减8000个。奥迪大幅下调营业额和利润预期，梅赛德斯-奔驰也下调了全年业绩预期。</p>
<p>这些都是德国去工业化的明显信号，也反映出德国经济模式在与中国日益激烈的竞争中所面临的压力。中国已形成大规模工业产能过剩，并凭借快速的技术追赶，开始冲击德国传统优势。</p>
<p>即便是在复杂工业品领域，中国企业也已经成为欧洲以及第三方市场上的顶尖竞争者，例如在电动汽车、电池、太阳能技术和机械设备等领域。同时，中国市场对德国产品的需求正在不断萎缩。不仅汽车制造商及其供应商需要作出调整，德国机械制造业同样面临巨大压力。</p>
]]></description>
<pubDate>Sat, 15 Aug 2026 10:43:37 GMT</pubDate>
</item>
</channel></rss>
"""


class TestSummaryAsContentFallback:
    """Phase 4：content 缺失但 summary 携带足够长 HTML 正文 → 直接用 summary。"""

    def test_discover_uses_summary_as_content_html(self):
        """content 缺失 + summary 有效 → content_html 被设为 summary HTML。"""
        items = discover_from_rss(_SUMMARY_FULL_BODY_RSS)
        assert items
        it = items[0]
        assert it.content_html  # summary 被用作 content_html
        assert has_usable_content(it.content_html) is True
        assert "德国汽车工业在7月底宣布大规模裁员" in it.content_html

    def test_pipeline_short_circuit_zero_fetch_from_summary(self, storage):
        """content 缺失 + summary 有效 → pipeline 0 fetch（不请求原文）。"""
        fetcher = FakeFetcher(_SUMMARY_FULL_BODY_RSS, html=_ARTICLE_HTML)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        items = discover_from_rss(_SUMMARY_FULL_BODY_RSS)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 短路：0 次原文请求
        assert fetcher.calls == []
        assert stats.fetched_ok == 1
        assert stats.extracted_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_html != ""
        assert art.body_text != ""
        pipe.close()

    def test_rfi_summary_body_kept_figure_removed(self):
        """RFI summary：正文保留，figure/figcaption（版权说明）不进入 body_text。"""
        from news.discover import html_to_text

        items = discover_from_rss(_SUMMARY_FULL_BODY_RSS)
        it = items[0]
        body_text = html_to_text(it.content_html)
        # 正文保留
        assert "德国汽车工业在7月底宣布大规模裁员" in body_text
        assert "据德国《商报》报道" in body_text  # chapo 导语保留
        # 图片版权说明 / figcaption / img alt 不进入正文
        assert "REUTERS" not in body_text
        assert "Benoit" not in body_text
        assert "Volkswagen" not in body_text  # figcaption 文本
        assert "德国汽车制造商采用中国零部件" not in body_text  # img alt


class TestNoUsableContentFallsBackToHtmlFetch:
    """Phase 4：content 与 summary 都不可用 → 才走原站 HTML fetch fallback。"""

    def test_content_and_summary_unusable_fetches_html(self, storage):
        """content=None + summary 很短 → has_usable_content=False → fetch 原文。"""
        fetcher = FakeFetcher(_SUMMARY_RSS, html=_ARTICLE_HTML)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        # _SUMMARY_RSS 只给很短的导语，无 content → 必须 fetch 原文
        items = discover_from_rss(_SUMMARY_RSS)
        assert items and not items[0].content_html
        assert has_usable_content(items[0].content_html) is False
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 触发 fetch 原文 + adapter HTML fallback
        assert fetcher.calls == ["https://www.rfi.fr/cn/politics/20260815-yasukuni"]
        assert stats.fetched_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "这是从原文页面提取的正文第一段" in art.body_text
        pipe.close()


class TestRsshubFeedLevelFallback:
    """Phase 4：sites/rfi.yaml 两个 RSSHub 实例 + feed-level fallback。"""

    def test_adapter_reads_instances_from_config(self):
        """RfiAdapter 从站点配置读取 rsshub_instances。"""
        cfg = load_site_config("rfi")
        adapter = get_adapter(cfg)
        assert isinstance(adapter, RfiAdapter)
        assert adapter.rsshub_instances == [
            "https://rsshub.rssforever.com/rfi/cn",
            "https://rsshub.ktachibana.party/rfi/cn",
        ]

    def test_first_instance_succeeds_no_second_request(self):
        """第一个 RSSHub 实例成功 → 不请求第二个实例。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_FULL_BODY_RSS)
        # 官方 RSS 不可达，走 RSSHub
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        # 请求了官方 RSS + 第一个 RSSHub 实例；没请求第二个实例
        assert adapter.rsshub_instances[0] in fetcher.calls
        assert adapter.rsshub_instances[1] not in fetcher.calls

    def test_first_instance_fails_then_second(self):
        """第一个实例整个失败（首页 + 各分类） → 切到第二个实例聚合。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_FULL_BODY_RSS)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        # 第一个 RSSHub 实例的首页与所有分类都失败
        fetcher.fail_substrings.append("rsshub.rssforever.com")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        # 第一个实例首页与分类都被尝试过，第二个实例被请求且成功
        assert "https://rsshub.rssforever.com/rfi/cn" in fetcher.calls
        assert "https://rsshub.ktachibana.party/rfi/cn" in fetcher.calls
        assert any("ktachibana" in u and u != "https://rsshub.ktachibana.party/rfi/cn" for u in fetcher.calls)

    def test_all_instances_fail_returns_empty(self):
        """所有 RSSHub 实例都失败（首页 + 分类） → 返回空列表，不抛异常。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_FULL_BODY_RSS)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        fetcher.fail_substrings.extend(
            ["rsshub.rssforever.com", "rsshub.ktachibana.party"]
        )
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items == []


# ---------------------------------------------------------------------------
# Phase 7：RFI RSSHub 分类聚合
# ---------------------------------------------------------------------------


class TestRsshubCategoryAggregation:
    """Phase 7：RSSHub 首页 + RFI 中文分类页面聚合。"""

    INSTANCE = "https://rsshub.rssforever.com/rfi/cn"

    def _url_map(self):
        """首页 + 两个分类各含不同文章的 URL→内容映射。"""
        return {
            self.INSTANCE: _mk_rss([("首页文章", "https://www.rfi.fr/cn/france/20260815-home")]),
            f"{self.INSTANCE}/politique": _mk_rss(
                [("政治文章", "https://www.rfi.fr/cn/politique/20260815-pol")]
            ),
            f"{self.INSTANCE}/moyen-orient": _mk_rss(
                [("中东文章", "https://www.rfi.fr/cn/moyen-orient/20260815-mo")]
            ),
        }

    def test_requests_homepage_then_categories(self):
        """第一个 RSSHub 实例内依次请求首页 + 各分类页面，聚合去重。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=self._url_map())
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        # 首页与各分类都被请求
        assert self.INSTANCE in fetcher.calls
        assert f"{self.INSTANCE}/politique" in fetcher.calls
        assert f"{self.INSTANCE}/moyen-orient" in fetcher.calls
        # 聚合了首页与分类的文章
        titles = [it.title for it in items]
        assert "首页文章" in titles
        assert "政治文章" in titles
        assert "中东文章" in titles
        # 首页在分类之前
        assert fetcher.calls.index(self.INSTANCE) < fetcher.calls.index(f"{self.INSTANCE}/politique")

    def test_category_order_follows_config(self):
        """分类请求顺序与 RFI_CN_CATEGORIES 定义一致。"""
        from news.sources.rfi import RFI_CN_CATEGORIES

        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=self._url_map())
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        adapter.discover(fetcher=fetcher, max_items=10)
        cat_calls = [
            u for u in fetcher.calls
            if u.startswith(f"{self.INSTANCE}/") and "rsshub" in u
        ]
        # 只请求了 url_map 里实际配置的分类（未配置的返回兜底空 feed 也请求过）
        expected_prefix = [f"{self.INSTANCE}/{slug}" for slug in RFI_CN_CATEGORIES]
        for p in expected_prefix:
            assert p in fetcher.calls

    def test_cross_category_url_dedup(self):
        """跨分类 canonical URL 去重：同一文章出现在多分类只保留一份。"""
        dup_url = "https://www.rfi.fr/cn/chine/20260815-dup"
        url_map = {
            self.INSTANCE: _mk_rss([("首页重复", dup_url)]),
            f"{self.INSTANCE}/politique": _mk_rss([("政治重复", dup_url)]),
            f"{self.INSTANCE}/moyen-orient": _mk_rss(
                [("中东独立", "https://www.rfi.fr/cn/moyen-orient/20260815-mo")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        urls = [canonicalize_url(it.url) for it in items]
        assert len(urls) == len(set(urls))  # 无重复
        assert urls.count(canonicalize_url(dup_url)) == 1

    def test_dedup_ignores_tracking_params(self):
        """跨分类去重对带追踪参数的同一 URL 仍只保留一份。"""
        base = "https://www.rfi.fr/cn/chine/20260815-dup"
        tracked = base + "?utm_source=rss&utm_medium=feed"
        url_map = {
            self.INSTANCE: _mk_rss([("首页", base)]),
            f"{self.INSTANCE}/politique": _mk_rss([("政治", tracked)]),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        urls = [canonicalize_url(it.url) for it in items]
        assert len(urls) == 1

    def test_stops_at_max_items_no_more_requests(self):
        """达到 max_items 立即停止，不再请求后续分类。"""
        url_map = {
            self.INSTANCE: _mk_rss(
                [
                    ("首页A", "https://www.rfi.fr/cn/france/20260815-a"),
                    ("首页B", "https://www.rfi.fr/cn/france/20260815-b"),
                ]
            ),
            f"{self.INSTANCE}/politique": _mk_rss(
                [("政治C", "https://www.rfi.fr/cn/politique/20260815-c")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=2)
        # 首页就达到 2 条，不再请求任何分类
        assert len(items) == 2
        assert f"{self.INSTANCE}/politique" not in fetcher.calls

    def test_max_items_stop_mid_categories(self):
        """分类聚合过程中达到 max_items 即停止后续分类请求。"""
        url_map = {
            self.INSTANCE: _mk_rss([("首页", "https://www.rfi.fr/cn/france/20260815-home")]),
            f"{self.INSTANCE}/politique": _mk_rss(
                [("政治A", "https://www.rfi.fr/cn/politique/20260815-a"),
                 ("政治B", "https://www.rfi.fr/cn/politique/20260815-b")]
            ),
            f"{self.INSTANCE}/moyen-orient": _mk_rss(
                [("中东", "https://www.rfi.fr/cn/moyen-orient/20260815-mo")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=3)
        # 首页1 + 政治2 = 3 条，达到 max_items 后不再请求 moyen-orient
        assert len(items) == 3
        assert f"{self.INSTANCE}/moyen-orient" not in fetcher.calls
        # 政治两个分类都被请求到了（达到 max_items 前）
        assert f"{self.INSTANCE}/politique" in fetcher.calls

    def test_homepage_fails_still_aggregates_categories(self):
        """首页失败不中断实例，仍继续聚合分类。"""
        url_map = {
            f"{self.INSTANCE}/politique": _mk_rss(
                [("政治文章", "https://www.rfi.fr/cn/politique/20260815-pol")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        fetcher.fail_urls.add(self.INSTANCE)  # 首页失败
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        assert items
        assert any(it.title == "政治文章" for it in items)

    def test_all_categories_tried_below_max_items_returns_available(self):
        """所有分类都已尝试完成但仍不足 max_items → 返回已有的全部，不凑数。"""
        url_map = {
            self.INSTANCE: _mk_rss(
                [("首页文章", "https://www.rfi.fr/cn/france/20260815-home")]
            ),
            f"{self.INSTANCE}/politique": _mk_rss(
                [("政治文章", "https://www.rfi.fr/cn/politique/20260815-pol")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        # max_items=100 但只有 2 篇唯一文章 → 返回 2，不凑到 100
        items = adapter.discover(fetcher=fetcher, max_items=100)
        assert len(items) == 2
        titles = [it.title for it in items]
        assert "首页文章" in titles
        assert "政治文章" in titles

    def test_single_category_fails_continues_next(self):
        """单个分类失败只记录 warning，继续请求后续分类。"""
        url_map = {
            self.INSTANCE: _mk_rss(
                [("首页文章", "https://www.rfi.fr/cn/france/20260815-home")]
            ),
            f"{self.INSTANCE}/moyen-orient": _mk_rss(
                [("中东文章", "https://www.rfi.fr/cn/moyen-orient/20260815-mo")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        # 让 politique 分类失败，但 moyen-orient 应该仍被请求
        fetcher.fail_urls.add(f"{self.INSTANCE}/politique")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        # moyen-orient 仍被请求并成功聚合
        assert f"{self.INSTANCE}/moyen-orient" in fetcher.calls
        assert any(it.title == "中东文章" for it in items)
        # politique 失败但后续分类继续
        assert f"{self.INSTANCE}/politique" in fetcher.calls


class TestRsshubCategoryTwoInstanceFallback:
    """Phase 7：两个 RSSHub 实例保持 fallback（不合并），每个实例内分类聚合。"""

    INST1 = "https://rsshub.rssforever.com/rfi/cn"
    INST2 = "https://rsshub.ktachibana.party/rfi/cn"

    def test_first_instance_aggregates_and_second_not_used(self):
        """第一个实例聚合成功 → 不请求第二个实例。"""
        url_map = {
            self.INST1: _mk_rss([("首页", "https://www.rfi.fr/cn/france/20260815-home")]),
            f"{self.INST1}/politique": _mk_rss(
                [("政治", "https://www.rfi.fr/cn/politique/20260815-pol")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        assert items
        assert f"{self.INST1}/politique" in fetcher.calls
        # 第二个实例（首页/分类）均未被请求
        assert self.INST2 not in fetcher.calls

    def test_second_instance_also_aggregates_categories(self):
        """第一个实例失败后，第二个实例同样做首页+分类聚合。"""
        url_map = {
            self.INST2: _mk_rss([("备选首页", "https://www.rfi.fr/cn/france/20260815-bak")]),
            f"{self.INST2}/politique": _mk_rss(
                [("备选政治", "https://www.rfi.fr/cn/politique/20260815-bakpol")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        fetcher.fail_substrings.append("rsshub.rssforever.com")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        assert items
        assert f"{self.INST2}/politique" in fetcher.calls
        titles = [it.title for it in items]
        assert "备选首页" in titles
        assert "备选政治" in titles

    def test_categories_listed_at_least_required(self):
        """RFI_CN_CATEGORIES 至少包含要求的分类。"""
        from news.sources.rfi import RFI_CN_CATEGORIES

        cats = set(RFI_CN_CATEGORIES)
        required = {
            "politique",       # 政治
            "moyen-orient",    # 中东
            "international",   # 国际
            "taiwan",          # 港澳台
            "societe",         # 社会
            "economie",        # 经济
            "sports",          # 体育
            "afrique",         # 非洲
            "asie",            # 亚洲
            "europe",          # 欧洲
            "chine",           # 中国
        }
        assert required.issubset(cats)
