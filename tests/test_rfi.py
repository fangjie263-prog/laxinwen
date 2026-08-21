"""RFI（法广中文）Source Adapter 测试。

覆盖 RFI 中文官网栏目页聚合（主入口）、跨栏目去重、发布时间排序、
max_items 截断、栏目失败隔离、官网全部失败时 RSSHub fallback、
双 RSSHub 实例 fallback（不合并）、以及不使用 europe/chine 错误 slug。

同时保留既有验收项：
- RfiAdapter 注册与发现调用链
- HTML fallback（.t-content__chapo + .t-content__body）
- 完整正文短路（RSS content:encoded → 0 fetch）
- summary 携带完整正文 → 直接用 summary（0 fetch）
- has_usable_content 语义
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
    RFI_CN_CATEGORY_PAGES,
    extract_body_from_html,
    extract_title,
    _parse_date_from_url,
)
from news.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------------
# 测试 fixture 辅助
# ---------------------------------------------------------------------------


class FakeFetcher(BaseFetcher):
    """离线假抓取器：记录 calls，按 URL 返回对应内容。

    - ``rss`` / ``html``：兜底内容（任何含 rss/rsshub/feed 的 URL 返回 ``rss``，
      其余返回 ``html``）；
    - ``url_map``：可选，精确 URL → 内容，优先于兜底（用于栏目聚合测试，不同
      栏目返回不同 HTML）；
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
    """由 ``(title, link)`` 列表构造一个最小 RSS feed。"""
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


def _mk_category_html(items: list[tuple[str, str]]) -> str:
    """由 ``(title, url)`` 列表构造一个 RFI 栏目页 HTML（含文章链接）。"""
    cards = "".join(
        f'<div class="item"><a href="{url}">{title}</a></div>'
        for title, url in items
    )
    return (
        '<html><body>'
        '<h2>RFI 中文栏目</h2>'
        f'<div class="list">{cards}</div>'
        '</body></html>'
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
        assert len(items) >= 1
        # 官方 RSS 优先返回 summary 条目
        assert fetcher.calls[0] == "https://www.rfi.fr/zh/rss"


# ---------------------------------------------------------------------------
# 2. 官网栏目页聚合（主入口）
# ---------------------------------------------------------------------------


class TestOfficialCategoryAggregation:
    """Phase 8：RFI 中文官网栏目页聚合（主入口，不再依赖 RSSHub 主发现）。"""

    def _url_map(self):
        """首页 + 两个栏目各含不同文章，URL 自带不同日期。"""
        return {
            "https://www.rfi.fr/cn/": _mk_category_html(
                [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
            ),
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
            ),
            "https://www.rfi.fr/cn/中国": _mk_category_html(
                [("中国文章", "https://www.rfi.fr/cn/中国/20260815-china")]
            ),
        }

    def test_multi_category_aggregation(self):
        """多栏目聚合：首页 + 各栏目文章都被聚合。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=self._url_map())
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=100)
        titles = [it.title for it in items]
        assert "首页文章" in titles
        assert "政治文章" in titles
        assert "中国文章" in titles
        assert len(items) == 3

    def test_cross_category_url_dedup(self):
        """跨栏目 canonical URL 去重：同一文章出现在多栏目只保留一份。"""
        dup_url = "https://www.rfi.fr/cn/中国/20260817-dup"
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html(
                [("首页重复", dup_url)]
            ),
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [("政治重复", dup_url)]
            ),
            "https://www.rfi.fr/cn/中国": _mk_category_html(
                [("中国独立", "https://www.rfi.fr/cn/中国/20260816-indep")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=100)
        urls = [canonicalize_url(it.url) for it in items]
        assert len(urls) == len(set(urls))  # 无重复
        assert urls.count(canonicalize_url(dup_url)) == 1
        assert len(items) == 2  # dup_url + 中国独立

    def test_published_at_correct_sorting(self):
        """发布时间从新到旧排序（来自 URL 自带日期）。"""
        url_map = {
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [
                    ("旧文", "https://www.rfi.fr/cn/政治/20260810-old"),
                    ("新文", "https://www.rfi.fr/cn/政治/20260817-new"),
                ]
            ),
            "https://www.rfi.fr/cn/中国": _mk_category_html(
                [("中文", "https://www.rfi.fr/cn/中国/20260815-mid")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=100)
        assert items[0].title == "新文"  # 20260817
        assert items[1].title == "中文"  # 20260815
        assert items[2].title == "旧文"  # 20260810
        # published_at 单调递减
        dates = [it.published_at for it in items]
        assert dates == sorted(dates, reverse=True)

    def test_max_items_100_returns_at_most_100(self):
        """max_items=100 最多返回 100 篇（候选远多于 100）。"""
        url_map = {}
        # 生成 150 个候选（分布在首页与两个栏目，各 50 篇）
        for i in range(50):
            url_map.setdefault(
                "https://www.rfi.fr/cn/", []
            ).append(
                (f"首页{i}", f"https://www.rfi.fr/cn/中国/20260817-home-{i}")
            )
            url_map.setdefault(
                "https://www.rfi.fr/cn/政治", []
            ).append(
                (f"政治{i}", f"https://www.rfi.fr/cn/政治/20260816-pol-{i}")
            )
            url_map.setdefault(
                "https://www.rfi.fr/cn/中国", []
            ).append(
                (f"中国{i}", f"https://www.rfi.fr/cn/中国/20260815-china-{i}")
            )
        url_map = {k: _mk_category_html(v) for k, v in url_map.items()}
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=100)
        assert len(items) == 100  # 恰好截断到 100
        # 结果按发布时间从新到旧：首页(17日) > 政治(16日) > 中国(15日)
        assert items[0].title.startswith("首页")
        assert items[50].title.startswith("政治")
        assert items[100 - 1] is not None

    def test_single_category_failure_does_not_affect_others(self):
        """某个栏目失败不影响其他栏目聚合。"""
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html(
                [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
            ),
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
            ),
            "https://www.rfi.fr/cn/中国": _mk_category_html(
                [("中国文章", "https://www.rfi.fr/cn/中国/20260815-china")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        fetcher.fail_urls.add("https://www.rfi.fr/cn/政治")  # 政治栏目失败
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=100)
        titles = [it.title for it in items]
        assert "首页文章" in titles
        assert "中国文章" in titles
        assert "政治文章" not in titles  # 失败栏目不产生条目
        assert len(items) == 2

    def test_below_max_items_returns_all_available(self):
        """所有栏目都尝试完仍不足 max_items → 返回已有的全部，不凑数。"""
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html(
                [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
            ),
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
            ),
        }
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(url_map=url_map)
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        adapter = get_adapter(cfg)
        # max_items=100 但只有 2 篇唯一文章 → 返回 2，不凑到 100
        items = adapter.discover(fetcher=fetcher, max_items=100)
        assert len(items) == 2
        assert "首页文章" in [it.title for it in items]
        assert "政治文章" in [it.title for it in items]

    def test_category_pages_use_chinese_slugs(self):
        """栏目 URL 使用中文 slug，绝不含 europe/chine 英文 slug。"""
        for _, url in RFI_CN_CATEGORY_PAGES:
            assert "/cn/" in url
            assert not url.endswith("/europe")
            assert not url.endswith("/chine")
            assert "/rfi/cn/" not in url  # 不是 RSSHub route


# ---------------------------------------------------------------------------
# 3. 官网全部失败时 RSSHub fallback
# ---------------------------------------------------------------------------


class TestRsshubFallbackWhenOfficialFails:
    """官网栏目全部无法访问时，fallback 到 RSSHub（两个实例不合并）。"""

    def _fail_all_official(self, fetcher):
        """让官方 RSS 与所有官网栏目页都失败。"""
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

    def test_all_official_fail_then_rsshub(self):
        """官网全部失败 → RSSHub fallback 返回文章。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)
        self._fail_all_official(fetcher)
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        assert items[0].title == "高市致辞不谈反省 四阁僚参拜靖国神社"
        assert any("rsshub" in u for u in fetcher.calls)
        # 官网栏目页都被请求过（尝试后失败才进入 fallback）
        assert "https://www.rfi.fr/cn/" in fetcher.calls
        assert "https://www.rfi.fr/cn/政治" in fetcher.calls

    def test_rsshub_two_instances_fallback_not_merged(self):
        """第一个 RSSHub 实例失败 → 切到第二个；两个不合并。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)
        self._fail_all_official(fetcher)
        # 第一个 RSSHub 实例失败，第二个成功
        fetcher.fail_substrings.append("rsshub.rssforever.com")
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        # 两个实例都被请求
        assert "https://rsshub.rssforever.com/rfi/cn" in fetcher.calls
        assert "https://rsshub.ktachibana.party/rfi/cn" in fetcher.calls
        # 结果来自第二个实例
        assert items[0].title == "高市致辞不谈反省 四阁僚参拜靖国神社"

    def test_first_instance_succeeds_enough_items_skip_second(self):
        """第一个 RSSHub 实例成功且已有足够条目 → 不请求第二个实例。"""
        cfg = load_site_config("rfi")
        # RSS 包含 10 篇文章，max_items=5 → 第一个实例就足够
        rss_many = _mk_rss([
            (f"文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(10)
        ])
        fetcher = FakeFetcher(rss_many)
        self._fail_all_official(fetcher)
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        assert adapter.rsshub_instances[0] in fetcher.calls
        assert adapter.rsshub_instances[1] not in fetcher.calls

    def test_first_instance_insufficient_tries_second(self):
        """第一个 RSSHub 实例返回不足 max_items → 尝试第二个实例补充。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)  # 只有 1 篇文章
        self._fail_all_official(fetcher)
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items
        assert adapter.rsshub_instances[0] in fetcher.calls
        # 第一个实例只有 1 篇 < max_items=5 → 继续尝试第二个实例
        assert adapter.rsshub_instances[1] in fetcher.calls

    def test_all_instances_fail_returns_empty(self):
        """所有 RSSHub 实例也失败 → 返回空列表，不抛异常。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)
        self._fail_all_official(fetcher)
        fetcher.fail_substrings.extend(
            ["rsshub.rssforever.com", "rsshub.ktachibana.party"]
        )
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        assert items == []


# ---------------------------------------------------------------------------
# 4. 不使用 europe/chine 错误 slug
# ---------------------------------------------------------------------------


class TestNoWrongSlugs:
    def test_no_europe_or_chine_rsshub_slugs(self):
        """发现过程中绝不请求 /rfi/cn/europe 或 /rfi/cn/chine。"""
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(_SUMMARY_RSS)
        # 官方 RSS 与所有栏目页都失败，触发 RSSHub fallback
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)
        adapter = get_adapter(cfg)
        adapter.discover(fetcher=fetcher, max_items=5)
        assert not any("/rfi/cn/europe" in u for u in fetcher.calls)
        assert not any("/rfi/cn/chine" in u for u in fetcher.calls)

    def test_adapter_reads_instances_from_config(self):
        """RfiAdapter 从站点配置读取 rsshub_instances。"""
        cfg = load_site_config("rfi")
        adapter = get_adapter(cfg)
        assert isinstance(adapter, RfiAdapter)
        assert adapter.rsshub_instances == [
            "https://rsshub.rssforever.com/rfi/cn",
            "https://rsshub.ktachibana.party/rfi/cn",
        ]


# ---------------------------------------------------------------------------
# 5. 发布时间解析（URL 日期）
# ---------------------------------------------------------------------------


class TestPublishedAtParsing:
    def test_parse_date_from_url(self):
        """从 RFI 文章 URL 解析发布日期。"""
        dt = _parse_date_from_url("https://www.rfi.fr/cn/中国/20260817-abc")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 17
        assert dt.tzinfo is not None

    def test_parse_date_invalid_returns_none(self):
        """URL 无日期段 → 返回 None。"""
        assert _parse_date_from_url("https://www.rfi.fr/cn/") is None
        assert _parse_date_from_url("https://example.com/x") is None


# ---------------------------------------------------------------------------
# 6. has_usable_content 语义
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
        items = discover_from_rss(_FULL_RSS)
        assert items and items[0].content_html
        assert has_usable_content(items[0].content_html) is True


# ---------------------------------------------------------------------------
# 7. HTML fallback（RSS 只有 summary → fetch 原文 → 提取正文）
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
        items = discover_from_rss(_SUMMARY_RSS)
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
        assert stats.fetched_ok >= 1
        assert stats.extracted_ok >= 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "正文" in art.body_text
        pipe.close()


# ---------------------------------------------------------------------------
# 8. 完整正文短路（content:encoded → content_html → 0 fetch）
# ---------------------------------------------------------------------------


class TestContentShortCircuit:
    def test_short_circuit_zero_fetch(self, storage):
        """RSS content:encoded → content_html → has_usable_content=True →
        fetcher.calls == []（0 fetch）→ body_html != "" / body_text != ""。"""
        fetcher = FakeFetcher(_FULL_RSS)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
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
# 9. 完整正文短路作为公共 pipeline regression test（通用站点也受益）
# ---------------------------------------------------------------------------


class TestPipelineContentShortCircuitRegression:
    def test_generic_pipeline_short_circuit(self, storage):
        """通用站点（无 adapter）也可用 content_html 短路，0 fetch。"""
        fetcher = FakeFetcher(_FULL_RSS)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        items = discover_from_rss(_FULL_RSS)
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
        item = DiscoveredItem(
            url="https://www.rfi.fr/cn/france/20260815-heatwave",
            title="测试标题",
            content_html="<p>这是一句很短的导语，不构成完整正文。</p>",
        )
        stats = pipe._ingest_items([item], "rfi", "RFI", "zh", {}, [])
        assert fetcher.calls == ["https://www.rfi.fr/cn/france/20260815-heatwave"]
        assert stats.fetched_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "正文" in art.body_text
        pipe.close()


# ---------------------------------------------------------------------------
# 10. content 缺失但 summary 携带完整正文 → 直接用 summary 作 content_html
#     （0-fetch 短路）；figure/figcaption 图片区不进入 body_text。
# ---------------------------------------------------------------------------

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
        assert "德国汽车工业在7月底宣布大规模裁员" in body_text
        assert "据德国《商报》报道" in body_text  # chapo 导语保留
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
        items = discover_from_rss(_SUMMARY_RSS)
        assert items and not items[0].content_html
        assert has_usable_content(items[0].content_html) is False
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        assert fetcher.calls == ["https://www.rfi.fr/cn/politics/20260815-yasukuni"]
        assert stats.fetched_ok == 1
        art = storage.list_articles(limit=1)[0]
        assert art.body_text and "这是从原文页面提取的正文第一段" in art.body_text
        pipe.close()


# ---------------------------------------------------------------------------
# 11. RSS 第一优先级：RSS 优先，官网只是补充（核心原则）
# ---------------------------------------------------------------------------


class TestRssFirstPriority:
    """RSS 第一优先级，官网及其他来源只是 RSS 的补充。"""

    def _mk_url_map_with_categories(self):
        """构造官网栏目 url_map（含首页与政治、中国栏目）。"""
        return {
            "https://www.rfi.fr/cn/": _mk_category_html(
                [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
            ),
            "https://www.rfi.fr/cn/政治": _mk_category_html(
                [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
            ),
            "https://www.rfi.fr/cn/中国": _mk_category_html(
                [("中国文章", "https://www.rfi.fr/cn/中国/20260815-china")]
            ),
        }

    def test_rss_success_goes_to_categories_but_no_dup(self):
        """RSS 成功时，官网补充仍执行但 RSS 已有文章不去重。"""
        # RSS 有 2 篇，官网也有其中 1 篇 + 1 篇新的
        rss_feed = _mk_rss([
            ("RSS文章1", "https://www.rfi.fr/cn/中国/20260817-home"),
            ("RSS文章2", "https://www.rfi.fr/cn/政治/20260816-pol"),
        ])
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(
            rss=rss_feed,
            url_map={
                "https://www.rfi.fr/cn/": _mk_category_html(
                    [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
                ),
                "https://www.rfi.fr/cn/政治": _mk_category_html(
                    [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
                ),
                "https://www.rfi.fr/cn/中国": _mk_category_html(
                    [("中国新文章", "https://www.rfi.fr/cn/中国/20260815-new")]
                ),
            },
        )
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        urls = [canonicalize_url(it.url) for it in items]
        # 包含 RSS 2 篇 + 官网新增 1 篇
        assert len(items) == 3
        assert "https://www.rfi.fr/cn/中国/20260817-home" in urls
        assert "https://www.rfi.fr/cn/政治/20260816-pol" in urls
        assert "https://www.rfi.fr/cn/中国/20260815-new" in urls
        # 无重复
        assert len(urls) == len(set(urls))

    def test_rss_success_no_dup_from_categories(self):
        """RSS 已有文章在官网出现时不会被重复添加。"""
        rss_feed = _mk_rss([
            ("RSS文章", "https://www.rfi.fr/cn/中国/20260817-home"),
        ])
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(
            rss=rss_feed,
            url_map={
                "https://www.rfi.fr/cn/": _mk_category_html(
                    [("首页文章", "https://www.rfi.fr/cn/中国/20260817-home")]
                ),
                "https://www.rfi.fr/cn/政治": _mk_category_html(
                    [("政治文章", "https://www.rfi.fr/cn/政治/20260816-pol")]
                ),
                "https://www.rfi.fr/cn/中国": _mk_category_html(
                    [("中国文章", "https://www.rfi.fr/cn/中国/20260815-china")]
                ),
            },
        )
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        urls = [canonicalize_url(it.url) for it in items]
        # RSS 1 篇 + 官网新增 2 篇（政治/中国），首页文章与 RSS 重复不计入
        assert len(items) == 3
        assert urls.count(canonicalize_url("https://www.rfi.fr/cn/中国/20260817-home")) == 1

    def test_rss_enough_items_no_categories_needed(self):
        """RSS 已提供足够多文章时，不请求官网栏目（降低反爬风险）。"""
        # RSS 提供 15 篇（max_items=10 足够）
        rss_feed = _mk_rss([
            (f"RSS文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(15)
        ])
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(rss=rss_feed)
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=10)
        # 只返回 RSS 的 10 篇
        assert len(items) == 10
        # RSS 已足够 → 不请求任何官网栏目页
        assert not any("www.rfi.fr/cn/" in u for u in fetcher.calls)

    def test_rss_success_with_limit_exact(self):
        """limit=max_items：RSS 找到 50 篇，官网补充，排序后取最新 N 篇。"""
        # RSS 提供 3 篇，官网补充 2 篇新文章
        rss_feed = _mk_rss([
            ("RSS新", "https://www.rfi.fr/cn/中国/20260817-new"),
            ("RSS中", "https://www.rfi.fr/cn/政治/20260816-mid"),
            ("RSS旧", "https://www.rfi.fr/cn/法国/20260810-old"),
        ])
        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(
            rss=rss_feed,
            url_map={
                "https://www.rfi.fr/cn/": _mk_category_html(
                    [("官网A", "https://www.rfi.fr/cn/中国/20260818-official-a")]
                ),
                "https://www.rfi.fr/cn/政治": _mk_category_html(
                    [("官网B", "https://www.rfi.fr/cn/政治/20260815-official-b")]
                ),
                "https://www.rfi.fr/cn/中国": _mk_category_html(
                    [("RSS新", "https://www.rfi.fr/cn/中国/20260817-new")]
                ),
            },
        )
        adapter = get_adapter(cfg)
        items = adapter.discover(fetcher=fetcher, max_items=5)
        # RSS 3 篇 + 官网新增 2 篇（A 和 B），共 5 篇
        assert len(items) == 5
        # 按时间排序，最新的在第一个（20260818 > 20260817 > 20260816 > ...）
        assert items[0].title == "官网A"  # 20260818
        assert items[1].title == "RSS新"  # 20260817
        assert items[2].title == "RSS中"  # 20260816

    def test_rss_unavailable_logs_fallback(self, caplog):
        """RSS 不可用时记录明确的 fallback 日志。"""
        import logging

        cfg = load_site_config("rfi")
        fetcher = FakeFetcher(
            rss="",
            url_map=self._mk_url_map_with_categories(),
        )
        fetcher.fail_urls.add("https://www.rfi.fr/zh/rss")
        fetcher.fail_substrings.append("rsshub")  # 所有 RSSHub 也失败
        adapter = get_adapter(cfg)
        with caplog.at_level(logging.WARNING, logger="news.sources.rfi"):
            items = adapter.discover(fetcher=fetcher, max_items=10)
        assert items  # 官网栏目仍然提供了文章
        # 日志中包含明确的 fallback 信息
        assert any("[RFI] RSS 官方不可用 → 启用官网补充模式" in r.message for r in caplog.records)

    def test_rss_has_full_body_does_not_fetch_article_page(self, storage):
        """RSS 完整正文 → 直接入库，不请求官网文章页。"""
        fetcher = FakeFetcher(_FULL_RSS, html=_ARTICLE_HTML)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=3)
        items = discover_from_rss(_FULL_RSS)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 完整正文 → 0 fetch
        assert fetcher.calls == []
        assert stats.fetched_ok == 1
        assert stats.usable == 1
        pipe.close()

    def test_short_rss_body_not_filtered(self):
        """RSS 正文即使较短（但确实是正文）也不应被高阈值过滤。"""
        # 单段落但较长（> 80 字）→ 应视为可用正文
        from news.discover import has_usable_content

        # 一个段落但内容较长（> 80 字）→ True
        long_single_para = "<p>这是一段较长的正文内容，虽然不是多段落结构，但包含了足够多的可读信息，长度远超 80 字符的阈值，所以应该被视为有效正文而不是摘要。</p>"
        assert has_usable_content(long_single_para) is True

        # 只有一个短段落（导语/摘要）→ False
        short_single_para = "<p>这只是一句导语。</p>"
        assert has_usable_content(short_single_para) is False
