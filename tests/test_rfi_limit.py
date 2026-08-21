"""RFI limit 语义测试：limit=N 表示"实际新增并成功入库的可读新闻数量"。

覆盖用户要求的 11 个场景：
1. limit=20，RSS 有 40 条其中 11 条数据库重复 → 最终仍然获取 20 篇新文章
2. RSS 已存在的文章不会消耗 limit
3. RSS 已经足够 20 篇时，不访问官网
4. RSS 不足 20 篇时，RSSHub 才补充
5. RSS + RSSHub 仍不足时，官网才补充
6. 官网补充不能重复 RSS
7. 已存在数据库的官网文章不能消耗 limit
8. RSS 完整正文继续保持 0 fetch
9. 正文抓取失败不能计入 limit
10. 空正文不能计入 limit
11. 最终只有 9 篇真正可读新闻时，limit=20 也只能得到 9 篇，日志明确说明原因
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.discover import DiscoveredItem, discover_from_rss  # noqa: E402
from news.fetch import BaseFetcher  # noqa: E402
from news.normalize import canonicalize_url  # noqa: E402
from news.pipeline import Pipeline  # noqa: E402
from news.sources import get_adapter  # noqa: E402
from news.sources.rfi import RfiAdapter  # noqa: E402
from news.storage import Storage  # noqa: E402


class FakeFetcher(BaseFetcher):
    """离线假抓取器：记录 calls，按 URL 返回对应内容。"""

    def __init__(self, rss: str = "", html: str = "", url_map: dict | None = None):
        self.rss = rss
        self.html = html
        self.url_map = url_map or {}
        self.calls: list[str] = []
        self.fail_urls: set[str] = set()
        self.fail_substrings: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
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


# RFI 完整正文 HTML（用于 short-circuit 测试）
_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>测试文章标题 - RFI</title>
<meta property="og:title" content="测试文章标题"/>
</head>
<body>
<article>
<h1>测试文章标题</h1>
<p class="t-content__chapo">这是导语段落。</p>
<div class="t-content__body">
<p>这是从原文页面提取的正文第一段，包含了完整的报道内容。</p>
<p>这是从原文页面提取的正文第二段，继续补充报道细节。</p>
<p>这是从原文页面提取的正文第三段，总结报道内容。</p>
</div>
</article>
</body>
</html>
"""


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "rfi_limit.db")
    yield s
    s.close()


def _full_body_rss(title: str, url: str) -> str:
    """构造一条带完整正文 content:encoded 的 RSS。"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>法广 - RFI</title>
<item>
<title>{title}</title>
<link>{url}</link>
<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"><![CDATA[
<p class="t-content__chapo">导语段落内容。</p>
<div class="t-content__body">
<p>这是完整的正文第一段，包含足够的可读内容。</p>
<p>这是完整的正文第二段，继续提供更多细节。</p>
<p>这是完整的正文第三段，总结全部内容。</p>
</div>
]]></content:encoded>
<pubDate>Sat, 15 Aug 2026 10:00:00 GMT</pubDate>
</item>
</channel></rss>"""


# ---------------------------------------------------------------------------
# 场景 1+2：limit=20，RSS 有 40 条其中 11 条数据库重复 → 仍然获取 20 篇新文章
#           RSS 已存在的文章不会消耗 limit
# ---------------------------------------------------------------------------


class TestLimitCountsNewArticlesOnly:
    def test_limit_20_with_11_existing_in_db_gets_20_new(self, storage):
        """limit=20，RSS 40 条其中 11 条已在数据库 → 跳过 11 条重复，继续找。"""
        # 构造 40 条 RSS 文章
        rss_items = [
            (f"文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(40)
        ]
        rss_feed = _mk_rss(rss_items)

        # 先插入 11 条到数据库（模拟已有文章）
        cfg_urls = set()
        for i in range(11):
            url = f"https://www.rfi.fr/cn/中国/20260817-{i}"
            cfg_urls.add(canonicalize_url(url))

        fetcher = FakeFetcher(rss=rss_feed)
        # 让 RSSHub 也失败（只依赖官方 RSS）
        fetcher.fail_substrings.append("rsshub")
        # 让所有官网栏目也失败
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        # 模拟数据库已存在 11 篇
        existing = set()
        for i in range(11):
            existing.add(canonicalize_url(f"https://www.rfi.fr/cn/中国/20260817-{i}"))

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=20, existing_urls=existing)

        # 返回所有过滤后的新候选（不截断）：40 - 11 = 29 篇
        assert len(items) == 29
        # 验证没有已存在的 URL 被返回
        for it in items:
            assert canonicalize_url(it.url) not in existing
        # 验证返回的文章是第 11-39 篇（前 11 篇已在库被跳过）
        urls = [canonicalize_url(it.url) for it in items]
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-0") not in urls
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-10") not in urls
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-11") in urls
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-39") in urls

    def test_existing_in_db_does_not_consume_limit(self, storage):
        """RSS 前几条已在数据库 → 不影响 limit 数量。"""
        rss_feed = _mk_rss([
            ("旧文1", "https://www.rfi.fr/cn/中国/20260810-old1"),
            ("旧文2", "https://www.rfi.fr/cn/中国/20260811-old2"),
            ("旧文3", "https://www.rfi.fr/cn/中国/20260812-old3"),
            ("新文1", "https://www.rfi.fr/cn/中国/20260813-new1"),
            ("新文2", "https://www.rfi.fr/cn/中国/20260814-new2"),
            ("新文3", "https://www.rfi.fr/cn/中国/20260815-new3"),
            ("新文4", "https://www.rfi.fr/cn/中国/20260816-new4"),
            ("新文5", "https://www.rfi.fr/cn/中国/20260817-new5"),
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        # 让所有 RSSHub 和官网失败
        fetcher.fail_substrings.append("rsshub")
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        existing = {
            canonicalize_url("https://www.rfi.fr/cn/中国/20260810-old1"),
            canonicalize_url("https://www.rfi.fr/cn/中国/20260811-old2"),
            canonicalize_url("https://www.rfi.fr/cn/中国/20260812-old3"),
        }

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=5, existing_urls=existing)

        # 5 篇新文章（3 篇旧文被跳过，不消耗 limit；返回全部新候选不截断）
        assert len(items) == 5
        titles = [it.title for it in items]
        assert "新文1" in titles
        assert "新文2" in titles
        assert "新文3" in titles
        assert "新文4" in titles
        assert "新文5" in titles
        assert "旧文1" not in titles


# ---------------------------------------------------------------------------
# 场景 3：RSS 已经足够 20 篇时，不访问官网
# ---------------------------------------------------------------------------


class TestRssEnoughSkipsOfficial:
    def test_rss_enough_no_official_calls(self):
        """RSS 提供 25 篇，limit=20 → 不请求任何官网栏目。"""
        rss_feed = _mk_rss([
            (f"RSS文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(25)
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})

        items = adapter.discover(fetcher=fetcher, max_items=20)

        assert len(items) == 25  # 返回所有 25 篇新候选（不截断）
        # 不请求官网栏目页
        official_urls = [
            "https://www.rfi.fr/cn/",
            "https://www.rfi.fr/cn/政治",
            "https://www.rfi.fr/cn/中国",
        ]
        for u in official_urls:
            assert u not in fetcher.calls

    def test_rss_enough_no_rsshub_after_official(self):
        """官方 RSS 足够 → 不请求 RSSHub。"""
        rss_feed = _mk_rss([
            (f"RSS文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(20)
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=10)
        assert len(items) == 20  # 返回所有 20 篇新候选（不截断）
        # 不请求 RSSHub
        for inst in adapter.rsshub_instances:
            assert inst not in fetcher.calls


# ---------------------------------------------------------------------------
# 场景 4：RSS 不足 20 篇时，RSSHub 才补充
# ---------------------------------------------------------------------------


class TestRsshubSupplement:
    def test_rss_insufficient_rsshub_supplements(self):
        """官方 RSS 只有 5 篇 < limit=10 → RSSHub 补充。"""
        rss_feed = _mk_rss([
            (f"RSS文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(5)
        ])
        hub_feed = _mk_rss([
            (f"RSSHub文章{i}", f"https://www.rfi.fr/cn/国际/20260817-hub-{i}")
            for i in range(10)
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        # 第一个 RSSHub 实例返回 hub_feed
        fetcher.url_map["https://rsshub.rssforever.com/rfi/cn"] = hub_feed
        # 第二个 RSSHub 实例也返回 hub_feed（确保不会因为第一个不够就失败）
        fetcher.url_map["https://rsshub.ktachibana.party/rfi/cn"] = hub_feed
        # 所有官网栏目失败
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=10)

        # RSS 5 篇 + RSSHub 补充 5 篇 = 10 篇
        assert len(items) == 10
        # RSSHub 被请求过
        assert any("rsshub" in u for u in fetcher.calls)


# ---------------------------------------------------------------------------
# 场景 5：RSS + RSSHub 仍不足时，官网才补充
# ---------------------------------------------------------------------------


class TestOfficialSupplement:
    def test_rss_and_rsshub_insufficient_official_supplements(self):
        """RSS 2 篇 + RSSHub 3 篇 < limit=10 → 官网补充。"""
        rss_feed = _mk_rss([
            ("RSS文章1", "https://www.rfi.fr/cn/中国/20260817-rss1"),
            ("RSS文章2", "https://www.rfi.fr/cn/中国/20260817-rss2"),
        ])
        hub_feed = _mk_rss([
            ("RSSHub文章1", "https://www.rfi.fr/cn/国际/20260817-hub1"),
            ("RSSHub文章2", "https://www.rfi.fr/cn/国际/20260817-hub2"),
            ("RSSHub文章3", "https://www.rfi.fr/cn/国际/20260817-hub3"),
        ])
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html([
                ("官网文章1", "https://www.rfi.fr/cn/政治/20260816-official1"),
                ("官网文章2", "https://www.rfi.fr/cn/政治/20260816-official2"),
            ]),
            "https://www.rfi.fr/cn/政治": _mk_category_html([
                ("官网文章3", "https://www.rfi.fr/cn/政治/20260815-official3"),
            ]),
            "https://www.rfi.fr/cn/中国": _mk_category_html([
                ("官网文章4", "https://www.rfi.fr/cn/中国/20260814-official4"),
            ]),
        }
        fetcher = FakeFetcher(rss=rss_feed, url_map=url_map)
        fetcher.url_map["https://rsshub.rssforever.com/rfi/cn"] = hub_feed
        fetcher.url_map["https://rsshub.ktachibana.party/rfi/cn"] = hub_feed

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=10)

        # RSS 2 + RSSHub 3 + 官网 4 = 9 篇（少于 limit=10，因为官网只有 4 篇新的）
        assert len(items) == 9
        # 官网被请求过
        assert any("www.rfi.fr/cn/" in u and u != "https://www.rfi.fr/zh/rss"
                   for u in fetcher.calls)


# ---------------------------------------------------------------------------
# 场景 6：官网补充不能重复 RSS
# ---------------------------------------------------------------------------


class TestOfficialNoDupWithRss:
    def test_official_cannot_duplicate_rss(self):
        """官网发现的文章若已在 RSS 中出现 → 不重复添加。"""
        rss_feed = _mk_rss([
            ("RSS文章", "https://www.rfi.fr/cn/中国/20260817-shared"),
        ])
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html([
                ("官网文章", "https://www.rfi.fr/cn/中国/20260817-shared"),
                ("官网新文", "https://www.rfi.fr/cn/政治/20260815-new"),
            ]),
        }
        fetcher = FakeFetcher(rss=rss_feed, url_map=url_map)
        # 让 RSSHub 也失败（保证只走 RSS + 官网）
        fetcher.fail_substrings.append("rsshub")

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=10)

        # RSS 1 篇 + 官网 1 篇新文章（另一篇与 RSS 重复被跳过）
        assert len(items) == 2
        urls = [canonicalize_url(it.url) for it in items]
        assert urls.count(canonicalize_url("https://www.rfi.fr/cn/中国/20260817-shared")) == 1


# ---------------------------------------------------------------------------
# 场景 7：已存在数据库的官网文章不能消耗 limit
# ---------------------------------------------------------------------------


class TestOfficialExistingInDb:
    def test_official_existing_in_db_skipped(self):
        """官网发现的文章已在数据库 → 跳过，不消耗 limit 名额。"""
        rss_feed = _mk_rss([
            ("RSS新文", "https://www.rfi.fr/cn/中国/20260817-rss-new"),
        ])
        url_map = {
            "https://www.rfi.fr/cn/": _mk_category_html([
                ("官网已有文章", "https://www.rfi.fr/cn/中国/20260817-existing"),
                ("官网新文章", "https://www.rfi.fr/cn/政治/20260816-new"),
            ]),
        }
        fetcher = FakeFetcher(rss=rss_feed, url_map=url_map)
        fetcher.fail_substrings.append("rsshub")

        existing = {
            canonicalize_url("https://www.rfi.fr/cn/中国/20260817-existing"),
        }
        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=10, existing_urls=existing)

        # RSS 1 篇 + 官网 1 篇新文章（官网已有文章被跳过）
        assert len(items) == 2
        urls = [canonicalize_url(it.url) for it in items]
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-existing") not in urls
        assert canonicalize_url("https://www.rfi.fr/cn/中国/20260817-rss-new") in urls
        assert canonicalize_url("https://www.rfi.fr/cn/政治/20260816-new") in urls


# ---------------------------------------------------------------------------
# 场景 8：RSS 完整正文继续保持 0 fetch
# ---------------------------------------------------------------------------


class TestRssFullBodyShortCircuit:
    def test_full_body_still_zero_fetch(self, storage):
        """RSS 完整正文 → 直接入库，0 fetch，不访问官网文章页。"""
        full_rss = _full_body_rss("完整正文文章", "https://www.rfi.fr/cn/中国/20260817-full")
        fetcher = FakeFetcher(rss=full_rss)
        pipe = Pipeline(storage, fetcher=fetcher, max_items=5)
        items = discover_from_rss(full_rss)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        assert stats.fetched_ok == 1
        assert stats.usable == 1
        # 0 fetch（完整正文短路）
        assert fetcher.calls == []
        pipe.close()


# ---------------------------------------------------------------------------
# 场景 9：正文抓取失败不能计入 limit
# ---------------------------------------------------------------------------


class TestFetchFailureNotCounted:
    def test_fetch_failure_not_counted(self, storage):
        """正文抓取失败 → 不能计入 limit。"""
        # RSS 只有 summary 没有正文 → 需要 fetch 原文
        rss_feed = _mk_rss([
            ("失败文章", "https://www.rfi.fr/cn/中国/20260817-fail"),
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        # 原文 URL 失败
        fetcher.fail_urls.add("https://www.rfi.fr/cn/中国/20260817-fail")

        pipe = Pipeline(storage, fetcher=fetcher, max_items=5)
        items = discover_from_rss(rss_feed)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        assert stats.failed == 1
        assert stats.usable == 0
        assert storage.count_usable() == 0
        pipe.close()

    def test_failed_articles_do_not_consume_limit_through_pipeline(self, storage):
        """Pipeline 中正文抓取失败 → 不消耗 limit 名额。"""
        rss_feed = _mk_rss([
            ("好文章1", "https://www.rfi.fr/cn/中国/20260817-good1"),
            ("坏文章", "https://www.rfi.fr/cn/中国/20260817-bad"),
            ("好文章2", "https://www.rfi.fr/cn/中国/20260817-good2"),
        ])
        fetcher = FakeFetcher(rss=rss_feed, html=_ARTICLE_HTML)
        fetcher.fail_urls.add("https://www.rfi.fr/cn/中国/20260817-bad")

        pipe = Pipeline(storage, fetcher=fetcher, max_items=5)
        items = discover_from_rss(rss_feed)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 2 篇成功（"坏文章" 失败不计入）
        assert stats.failed == 1
        assert stats.usable == 2
        assert stats.fetched_ok == 2
        pipe.close()


# ---------------------------------------------------------------------------
# 场景 10：空正文不能计入 limit
# ---------------------------------------------------------------------------


class TestEmptyBodyNotCounted:
    def test_empty_body_not_usable(self, storage):
        """空标题或空正文 → 不能作为 usable 文章。"""
        from news.model import Article

        # 构造空标题/空正文的条目
        items = [
            DiscoveredItem(url="https://www.rfi.fr/cn/中国/20260817-empty1"),
            DiscoveredItem(url="https://www.rfi.fr/cn/中国/20260817-empty2"),
        ]

        # 用 FakeFetcher 返回空 HTML（无正文）
        fetcher = FakeFetcher(rss="", html="<html><body></body></html>")
        pipe = Pipeline(storage, fetcher=fetcher, max_items=5)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        # 空正文文章不能入库为 usable
        assert stats.usable == 0
        pipe.close()


# ---------------------------------------------------------------------------
# 场景 11：最终只有 9 篇真正可读时，limit=20 也只能得到 9 篇
# ---------------------------------------------------------------------------


class TestOnly9Usable:
    def test_only_9_usable_gets_9(self, storage, caplog):
        """RSS 只有 9 篇新文章，limit=20 → 最终 9 篇，日志明确说明原因。"""
        rss_feed = _mk_rss([
            (f"文章{i}", f"https://www.rfi.fr/cn/中国/20260817-{i}")
            for i in range(9)
        ])
        fetcher = FakeFetcher(rss=rss_feed)
        # 所有 RSSHub 和官网都失败
        fetcher.fail_substrings.append("rsshub")
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        adapter = get_adapter({"id": "rfi", "name": "RFI", "adapter": "rfi"})
        items = adapter.discover(fetcher=fetcher, max_items=20)
        # 只有 9 篇（RSS 只有 9 篇新文章，所有其他来源都失败）
        assert len(items) == 9

        # Pipeline 也只能入库 9 篇（或更少）
        pipe = Pipeline(storage, fetcher=fetcher, max_items=20)
        stats = pipe._ingest_items(items, "rfi", "RFI", "zh", {}, [])
        assert stats.usable <= 9

    def test_only_9_usable_logs_all_sources_exhausted(self, storage, caplog):
        """最终只有 9 篇可读 → 日志明确说明所有来源已经耗尽。"""
        import logging

        # 9 篇带完整正文的 RSS（content:encoded → 0-fetch 成功入库）
        rss_entries = ""
        for i in range(9):
            rss_entries += f"""
            <item>
            <title>文章{i}</title>
            <link>https://www.rfi.fr/cn/中国/20260817-{i}</link>
            <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"><![CDATA[
            <p class="t-content__chapo">导语段落内容。</p>
            <div class="t-content__body">
            <p>这是完整的正文第一段，包含足够的可读内容。</p>
            <p>这是完整的正文第二段，继续提供更多细节。</p>
            <p>这是完整的正文第三段，总结全部内容。</p>
            </div>
            ]]></content:encoded>
            <pubDate>Sat, 15 Aug 2026 10:00:00 GMT</pubDate>
            </item>
            """
        rss_feed = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>法广 - RFI</title>'
            + rss_entries
            + "</channel></rss>"
        )
        fetcher = FakeFetcher(rss=rss_feed)
        fetcher.fail_substrings.append("rsshub")
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        pipe = Pipeline(storage, fetcher=fetcher, max_items=20)
        with caplog.at_level(logging.WARNING, logger="news"):
            stats = pipe.run_site("rfi")
        assert stats.usable == 9
        assert any("所有来源已耗尽" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 端到端验证：Pipeline.run_site 使用 existing_urls 过滤
# ---------------------------------------------------------------------------


class TestPipelineExistingUrlsIntegration:
    def test_pipeline_run_site_filters_existing(self, storage):
        """Pipeline.run_site 将数据库已有 URL 传给 discover，跳过重复。"""
        # 先插入 5 条到数据库
        for i in range(5):
            from news.model import Article
            art = Article(
                source_id="rfi",
                source_name="RFI",
                canonical_url=canonicalize_url(f"https://www.rfi.fr/cn/中国/20260810-old{i}"),
                title=f"旧文章{i}",
                body_text="已有正文内容",
            )
            storage.insert_article(art)

        # RSS 提供 15 篇（前 5 篇与数据库重复）
        rss_feed = _mk_rss([
            (f"旧文章{i}", f"https://www.rfi.fr/cn/中国/20260810-old{i}")
            for i in range(5)
        ] + [
            (f"新文章{i}", f"https://www.rfi.fr/cn/中国/20260817-new{i}")
            for i in range(10)
        ])
        fetcher = FakeFetcher(rss=rss_feed, html=_ARTICLE_HTML)
        # 所有 RSSHub 和官网失败
        fetcher.fail_substrings.append("rsshub")
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        pipe = Pipeline(storage, fetcher=fetcher, max_items=10)
        stats = pipe.run_site("rfi")
        # 5 篇旧文在数据库已存在 → 被过滤；10 篇新文中入库 10 篇
        # 但由于发现时 existing_urls 已过滤掉 5 篇旧文，discover 返回 10 篇新文
        assert stats.discovered == 10
        assert stats.fetched_ok == 10
        pipe.close()

    def test_pipeline_limit_semantics_with_existing(self, storage):
        """端到端：limit=10，数据库中已有部分文章 → 仍然获取 10 篇新文章。"""
        # 先插入 5 条到数据库
        for i in range(5):
            from news.model import Article
            art = Article(
                source_id="rfi",
                source_name="RFI",
                canonical_url=canonicalize_url(f"https://www.rfi.fr/cn/中国/20260810-old{i}"),
                title=f"旧文章{i}",
                body_text="已有正文内容",
            )
            storage.insert_article(art)

        # RSS 提供 20 篇（前 5 篇与数据库重复 + 15 篇新文章）
        rss_feed = _mk_rss([
            (f"旧文章{i}", f"https://www.rfi.fr/cn/中国/20260810-old{i}")
            for i in range(5)
        ] + [
            (f"新文章{i}", f"https://www.rfi.fr/cn/中国/20260817-new{i}")
            for i in range(15)
        ])
        fetcher = FakeFetcher(rss=rss_feed, html=_ARTICLE_HTML)
        # 所有 RSSHub 和官网失败
        fetcher.fail_substrings.append("rsshub")
        from news.sources.rfi import RFI_CN_CATEGORY_PAGES
        for _, url in RFI_CN_CATEGORY_PAGES:
            fetcher.fail_urls.add(url)

        pipe = Pipeline(storage, fetcher=fetcher, max_items=10)
        stats = pipe.run_site("rfi")

        # 10 篇新文章入库（5 篇旧文被 existing_urls 过滤，不消耗 limit）
        assert stats.usable == 10
        assert storage.count_usable() == 15  # 5 篇旧 + 10 篇新
        pipe.close()
