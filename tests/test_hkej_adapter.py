"""HKEJ Source Adapter 测试。

覆盖验收清单第十二节要求：

1. 列表页解析（正常 URL / 多 URL / 无效 URL / 重复 URL）
2. 标题 fallback（h1 / og:title / title / 全部不存在）
3. 正文解析（article-content，导航/广告/菜单不进入正文）
4. 分页（limit 20 / 50 / 100 正确停止）
5. URL 去重（同 URL / fragment / UTM / 重复列表页 / 跨页重复）
6. 数据库持久化去重（第一次新增，第二次重复，通过真实 SQLite）
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.fetch import BaseFetcher  # noqa: E402
from news.normalize import canonicalize_url  # noqa: E402
from news.sources.hkej import (  # noqa: E402
    HkejAdapter,
    extract_article_id,
    extract_author,
    extract_body,
    extract_title,
    parse_list_page,
)
from news.config import load_site_config  # noqa: E402
from news.discover import discover_for_site, DiscoveredItem  # noqa: E402

BASE = "https://www.hkej.com"


# ---------------------------------------------------------------------------
# 测试 fixture 辅助
# ---------------------------------------------------------------------------


def _article_url(article_id: int, title: str, cat: str = "hongkong") -> str:
    from urllib.parse import quote

    # 真实 HKEJ 列表页 href 为相对路径：/instantnews/<cat>/article/<id>/<encoded-title>
    return f"/instantnews/{cat}/article/{article_id}/{quote(title)}"


def _list_html(*links: str) -> str:
    """构造 HKEJ 列表页 HTML（真实页面为 <li class="hkej_hl-news_list_2014"><a href=...>）。"""
    items = "".join(
        f'<li class="hkej_hl-news_list_2014"><a href="{l}">新聞</a></li>'
        for l in links
    )
    return (
        "<html><head><title>即時新聞 - 信報網站 hkej.com</title></head>"
        f'<body><ul>{items}</ul>'
        '<nav><a href="/instantnews/index?page=2">下一頁</a></nav>'
        "</body></html>"
    )


def _article_html(
    title: str,
    body: str = "",
    *,
    h1: str | None = None,
    og: str | None = None,
    with_author: bool = False,
) -> str:
    """构造 HKEJ 文章页 HTML（真实页面结构：h1 + article-content div + 导航/菜单）。"""
    h1_html = f"<h1>{h1 or title}</h1>" if h1 is not None else ""
    og_html = f'<meta property="og:title" content="{og or ""}"/>' if og is not None else ""
    author_html = '<meta name="author" content="記者 王小明"/>' if with_author else ""
    body_html = (
        f'<div id="article-content" class="content">{body}</div>' if body else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh_HK">
<head>
<title>{title} - 信報網站 hkej.com</title>
{og_html}
{author_html}
</head>
<body>
<header><nav>導航菜單 | 首頁 | 財經 | 股市</nav></header>
<div class="article-wrap">
{h1_html}
{body_html}
</div>
<aside>廣告區塊</aside>
<footer>頁腳</footer>
</body>
</html>"""


class FakeHkejFetcher(BaseFetcher):
    """模拟 HKEJ：第 1 页 + 分页页，每页若干文章，可注入无效/重复链接。"""

    def __init__(self, per_page=20, total_pages=5, *, invalid=False, dup=False):
        self.per_page = per_page
        self.total_pages = total_pages
        self.invalid = invalid
        self.dup = dup
        self.calls: list[str] = []

    def _page_links(self, page: int) -> list[str]:
        start = (page - 1) * self.per_page + 1
        links = [_article_url(1000 + i, f"新聞{i}") for i in range(start, start + self.per_page)]
        if self.dup:
            # 每页重复前两条（测试同页/跨页去重）
            links = links[:2] * 1 + links
        if self.invalid:
            links = [
                "/instantnews/stock/article/not-a-number/壞鏈接",
                "https://www.hkej.com/instantnews/hongkong/article/0/",
                "/instantnews/stock/article/",
                "javascript:void(0)",
                "#",
            ] + links
        return links

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        if url == f"{BASE}/instantnews":
            return _list_html(*self._page_links(1))
        if url.startswith(f"{BASE}/instantnews/index?page="):
            page = int(url.split("page=")[1])
            if page > self.total_pages:
                return "<html><body></body></html>"
            return _list_html(*self._page_links(page))
        raise AssertionError(f"Unexpected URL: {url}")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. 列表页解析
# ---------------------------------------------------------------------------


class TestListPageParsing:
    def test_normal_article_links(self):
        links = parse_list_page(
            _list_html(
                "/instantnews/hongkong/article/1001/%E6%96%B0%E8%81%9E1",
                "/instantnews/stock/article/1002/%E6%96%B0%E8%81%9E2",
            )
        )
        assert len(links) == 2
        assert links[0].startswith("/instantnews/hongkong/article/1001/")
        assert links[1].startswith("/instantnews/stock/article/1002/")

    def test_multiple_links_kept_in_order(self):
        urls = [
            "/instantnews/hongkong/article/1001/a",
            "/instantnews/stock/article/1002/b",
            "/instantnews/china/article/1003/c",
        ]
        links = parse_list_page(_list_html(*urls))
        assert links == urls

    def test_invalid_links_filtered_by_regex(self):
        links = parse_list_page(
            _list_html(
                "/instantnews/hongkong/article/1001/%E6%96%B0%E8%81%9E1",
                "/about",
                "https://www.hkej.com/",
                "javascript:void(0)",
                "/instantnews/hongkong/article/abc/not-number",
            )
        )
        # LINK_RE 只匹配 /instantnews/<cat>/article/<digits>/，其余被过滤
        assert len(links) == 1
        assert links[0].startswith("/instantnews/hongkong/article/1001/")

    def test_duplicate_links_deduped(self):
        url = "/instantnews/hongkong/article/1001/%E6%96%B0%E8%81%9E1"
        links = parse_list_page(_list_html(url, url, url))
        assert len(links) == 1

    def test_empty_page_returns_empty(self):
        assert parse_list_page("<html></html>") == []


# ---------------------------------------------------------------------------
# 2. 标题 fallback
# ---------------------------------------------------------------------------


class TestTitleFallback:
    def test_h1_priority(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="OG標題"/>'
            "<title>Title標題 - 信報網站 hkej.com</title>"
            "</head><body><h1>H1標題</h1></body></html>"
        )
        assert extract_title(html) == "H1標題"

    def test_og_title_when_no_h1(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="OG標題"/>'
            "<title>Title標題 - 信報網站 hkej.com</title>"
            "</head><body></body></html>"
        )
        assert extract_title(html) == "OG標題"

    def test_title_tag_as_last_fallback(self):
        html = "<html><head><title>純標題 - 信報網站 hkej.com</title></head><body></body></html>"
        assert extract_title(html) == "純標題"

    def test_all_missing_returns_empty(self):
        assert extract_title("<html><body>no title</body></html>") == ""

    def test_h1_with_inner_tags_stripped(self):
        html = "<html><body><h1><b>加粗</b> 標題<br></h1></body></html>"
        assert extract_title(html) == "加粗 標題"


# ---------------------------------------------------------------------------
# 3. 正文解析
# ---------------------------------------------------------------------------


class TestBodyParsing:
    def test_extracts_article_content(self):
        html = _article_html("標題", body="<p>第一段正文。</p><p>第二段正文。</p>")
        body = extract_body(html)
        assert "第一段正文" in body
        assert "第二段正文" in body

    def test_navigation_advertisement_menu_not_included(self):
        html = (
            "<html><body>"
            "<header><nav>導航菜單 | 首頁 | 財經 | 股市</nav></header>"
            '<div id="article-content"><p>這是正文。</p></div>'
            "<aside>廣告區塊 banner</aside>"
            "<footer>頁腳</footer>"
            "</body></html>"
        )
        body = extract_body(html)
        assert "這是正文" in body
        assert "導航菜單" not in body
        assert "廣告區塊" not in body
        assert "頁腳" not in body

    def test_script_style_skipped(self):
        html = (
            '<div id="article-content">'
            "<script>var tracking=1;</script>"
            "<p>正文段落。</p>"
            "<style>.ad{color:red}</style>"
            "<p>更多正文。</p>"
            "</div>"
        )
        body = extract_body(html)
        assert "正文段落" in body
        assert "tracking" not in body
        assert ".ad" not in body

    def test_missing_article_content_returns_empty(self):
        assert extract_body("<html><body>只有正文文本</body></html>") == ""

    def test_author_extraction(self):
        html = _article_html("標題", body="正文", with_author=True)
        assert extract_author(html) == ["記者 王小明"]

    def test_author_missing_returns_empty(self):
        html = _article_html("標題", body="正文", with_author=False)
        assert extract_author(html) == []


# ---------------------------------------------------------------------------
# 4. 分页
# ---------------------------------------------------------------------------


class TestPagination:
    def _discover(self, fetcher, limit):
        cfg = load_site_config("hkej")
        adapter = HkejAdapter("hkej", cfg["name"])
        items = adapter.discover(fetcher=fetcher, max_items=limit)
        return items, fetcher.calls

    def test_limit_20_stops_at_20(self):
        fetcher = FakeHkejFetcher(per_page=20, total_pages=5)
        items, calls = self._discover(fetcher, 20)
        assert len(items) == 20
        # 只请求了第 1 页
        assert calls == [f"{BASE}/instantnews"]

    def test_limit_50_two_pages(self):
        fetcher = FakeHkejFetcher(per_page=20, total_pages=5)
        items, calls = self._discover(fetcher, 50)
        assert len(items) == 50
        # 第 1 页 20 条 + 第 2 页 20 条 + 第 3 页 10 条
        assert calls == [
            f"{BASE}/instantnews",
            f"{BASE}/instantnews/index?page=2",
            f"{BASE}/instantnews/index?page=3",
        ]

    def test_limit_100_five_pages(self):
        fetcher = FakeHkejFetcher(per_page=20, total_pages=5)
        items, calls = self._discover(fetcher, 100)
        assert len(items) == 100
        assert len(calls) == 5
        assert calls[0] == f"{BASE}/instantnews"
        assert calls[-1] == f"{BASE}/instantnews/index?page=5"

    def test_stops_when_pages_exhausted(self):
        fetcher = FakeHkejFetcher(per_page=20, total_pages=2)
        items, calls = self._discover(fetcher, 100)
        # 只有 2 页共 40 条；第 3 页为空页用于探测结束（不再新增）
        assert len(items) == 40
        assert calls[0] == f"{BASE}/instantnews"
        assert calls[1] == f"{BASE}/instantnews/index?page=2"
        assert calls[2] == f"{BASE}/instantnews/index?page=3"

    def test_discover_for_site_uses_adapter(self):
        """discover_for_site 应能按 hkej 配置分发到 adapter。"""
        fetcher = FakeHkejFetcher(per_page=20, total_pages=3)
        cfg = load_site_config("hkej")
        items = discover_for_site(cfg, fetcher=fetcher, max_items=50)
        assert len(items) == 50
        assert isinstance(items[0], DiscoveredItem)
        assert items[0].url.startswith(BASE)
        # 标题应从 URL 解码填充
        assert items[0].title


# ---------------------------------------------------------------------------
# 5. URL 去重
# ---------------------------------------------------------------------------


class TestUrlDedup:
    def test_same_url_deduped(self):
        url = "/instantnews/hongkong/article/1001/%E6%96%B0%E8%81%9E1"
        links = parse_list_page(_list_html(url, url))
        assert len(links) == 1

    def test_duplicate_across_pages(self):
        """跨页重复：第 2 页与第 1 页相同的文章 ID 不重复收集。"""
        urls1 = [_article_url(1001, "新聞1")]
        urls2 = [_article_url(1001, "新聞1")]  # 同 URL

        class DupFetcher(BaseFetcher):
            def __init__(self):
                self.calls = []

            def fetch(self, url, **kwargs):
                self.calls.append(url)
                if url == f"{BASE}/instantnews":
                    return _list_html(*urls1)
                return _list_html(*urls2)

            def close(self):
                pass

        cfg = load_site_config("hkej")
        adapter = HkejAdapter("hkej", cfg["name"])
        items = adapter.discover(fetcher=DupFetcher(), max_items=10)
        assert len(items) == 1

    def test_canonicalize_handles_fragment_and_utm(self):
        """URL fragment / UTM 参数在 canonical 层被去除，不影响去重。"""
        base = _article_url(1001, "新聞1")
        assert canonicalize_url(base + "#frag") == base
        assert canonicalize_url(base + "?utm_source=x&utm_medium=y") == base
        assert canonicalize_url(base + "#frag?utm_source=x") == base

    def test_fragment_utm_urls_deduped_in_discovery(self):
        url1 = _article_url(1001, "新聞1")
        url2 = url1 + "#section"
        url3 = url1 + "?utm_source=rss"

        class FragFetcher(BaseFetcher):
            def __init__(self):
                self.calls = []

            def fetch(self, url, **kwargs):
                self.calls.append(url)
                return _list_html(url1, url2, url3)

            def close(self):
                pass

        cfg = load_site_config("hkej")
        adapter = HkejAdapter("hkej", cfg["name"])
        items = adapter.discover(fetcher=FragFetcher(), max_items=10)
        assert len(items) == 1
        # 相对 URL 被解析为绝对 URL，且 canonical 去重（fragment/UTM 已去除）
        assert items[0].url == canonicalize_url(BASE + url1)

    def test_invalid_urls_do_not_produce_items(self):
        fetcher = FakeHkejFetcher(per_page=5, total_pages=1, invalid=True)
        cfg = load_site_config("hkej")
        adapter = HkejAdapter("hkej", cfg["name"])
        items = adapter.discover(fetcher=fetcher, max_items=10)
        # 无效链接（非文章模式 / 0-id / javascript / #）被 LINK_RE 过滤，只剩正常 5 条
        assert all(extract_article_id(it.url) for it in items)
        assert len(items) == 5


# ---------------------------------------------------------------------------
# 6. 数据库持久化去重（真实 SQLite）
# ---------------------------------------------------------------------------


class TestDatabaseDedup:
    @pytest.fixture
    def storage(self, tmp_path):
        from news.storage import Storage

        s = Storage(tmp_path / "hkej.db")
        yield s
        s.close()

    def _run_fetch(self, storage, fetcher, limit):
        from news.pipeline import Pipeline

        pipe = Pipeline(storage, fetcher=fetcher, max_items=limit)
        # 走完整 run_site（含 adapter 分发 + 下载 + 提取 + 入库）
        stats = pipe.run_site("hkej")
        pipe.close()
        return stats

    def test_first_run_inserts_second_run_zero_new(self, storage):
        """第一次抓取新增约 limit 篇；第二次抓取（相同 URL）全部去重，新增 0。"""
        fetcher1 = FakeHkejFetcher(per_page=20, total_pages=3)
        stats1 = self._run_fetch(storage, fetcher1, 50)
        # FakeFetcher 的 _article_html 是列表页 HTML，正文提取可能为空
        # 但入库（new/fetched/failed）以 discovered 为准
        assert stats1.discovered == 50
        assert storage.count(source_id="hkej") == 50
        # 第二次：相同内容重新发现
        fetcher2 = FakeHkejFetcher(per_page=20, total_pages=3)
        stats2 = self._run_fetch(storage, fetcher2, 50)
        assert stats2.discovered == 50
        assert stats2.skipped_dup == 50
        assert stats2.fetched_ok == 0
        assert storage.count(source_id="hkej") == 50

    def test_second_run_with_url_variants_deduped(self, storage):
        """第二次以带 fragment/UTM 的 URL 出现时，canonical 去重仍生效。"""
        base_links = [_article_url(1001, "新聞1"), _article_url(1002, "新聞2")]

        class PlainFetcher(BaseFetcher):
            def fetch(self, url, **kwargs):
                return _list_html(*base_links)

            def close(self):
                pass

        from news.pipeline import Pipeline

        pipe = Pipeline(storage, fetcher=PlainFetcher(), max_items=10)
        stats1 = pipe.run_site("hkej")
        assert storage.count(source_id="hkej") == 2
        pipe.close()

        # 第二次 URL 带 UTM/fragment
        variant_links = [
            base_links[0] + "?utm_source=rss&utm_campaign=test",
            base_links[1] + "#top",
        ]

        class VariantFetcher(BaseFetcher):
            def fetch(self, url, **kwargs):
                return _list_html(*variant_links)

            def close(self):
                pass

        pipe2 = Pipeline(storage, fetcher=VariantFetcher(), max_items=10)
        stats2 = pipe2.run_site("hkej")
        assert stats2.skipped_dup == 2
        assert storage.count(source_id="hkej") == 2
        pipe2.close()

    def test_article_id_extraction(self):
        assert extract_article_id(f"{BASE}/instantnews/stock/article/4453778/foo") == "4453778"
        assert extract_article_id(f"{BASE}/instantnews/hongkong/article/1/") == "1"
        assert extract_article_id(f"{BASE}/instantnews/") is None
        assert extract_article_id("https://example.com/not-hkej") is None
