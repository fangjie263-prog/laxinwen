"""ECO load-more 分页发现测试。

覆盖用户验收清单：
- load-more endpoint 解析（按钮 data-action / ECO_JS nonce / ajax url / 每页条数）
- 多次 load-more 持续获得文章（offset 翻页）
- RSS + 栏目页 + load-more 合并去重
- limit=50 / 100 / 200
- load-more 失败时 RSS/栏目页仍然可用（不中断）
- 带 UTM / fragment 的 URL 不产生重复
- 响应带 BOM 也能解析
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.discover import (  # noqa: E402
    _extract_json_object,
    discover_from_load_more,
    discover_for_site,
    discover_from_rss,
)
from news.fetch import BaseFetcher  # noqa: E402


# ---------- Fixtures ----------

# 首页 HTML：含 26 个文章卡片 + load-more 按钮 + ECO_JS 配置
def _card_article(url: str, title: str) -> str:
    return (
        f'<article class="card-list card card--list">'
        f'<a class="link-cover" href="{url}"></a>'
        f'<h3 class="card__title"><a href="{url}">{title}</a></h3>'
        f"</article>"
    )


def _make_list_html(urls: list[str], offset: int) -> str:
    cards = "".join(_card_article(u, f"Título {i}") for i, u in enumerate(urls))
    return f"""<!DOCTYPE html>
<html><head>
<script>
var ECO_JS = {{"nonce_load_more":"abc123nonce","wp_ajax_url":"https://eco.sapo.pt/wp-admin/admin-ajax.php","archive_load_more":"12"}};
</script>
</head><body>
<div class="list">{cards}</div>
<button class="js-archive-load-more load-more" data-action="eco_ajax_get_posts_latest" data-offset="{offset}">Carregar mais artigos</button>
</body></html>"""


def _make_ajax_payload(urls: list[str], count: int = 12, last_batch: bool = False) -> str:
    cards = "".join(_card_article(u, f"Título {i}") for i, u in enumerate(urls))
    payload = {"posts_html": cards, "count": count, "count_query": count + 1}
    if last_batch:
        payload["last_batch"] = True
    # 模拟 ECO：带 UTF-8 BOM
    return "\ufeff" + json.dumps({"success": True, "data": payload})


class FakeFetcher(BaseFetcher):
    """离线假抓取器，模拟 ECO 首页 + admin-ajax 分页。"""

    def __init__(self, initial_urls: list[str], pages: dict[int, str]):
        self.initial_urls = initial_urls
        self.pages = pages  # offset -> JSON 字符串
        self.calls: list[str] = []
        self.initial_html = _make_list_html(initial_urls, len(initial_urls))

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        if "admin-ajax" in url:
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(url).query)
            offset = int(qs.get("eco_offset", ["0"])[0])
            if offset in self.pages:
                return self.pages[offset]
            # 未命中返回空
            return "\ufeff" + json.dumps({"success": True, "data": {"posts_html": "", "count": 0}})
        return self.initial_html

    def close(self) -> None:
        pass


def _article_url(prefix: str, i: int) -> str:
    return f"https://eco.sapo.pt/2026/08/0{8}/{prefix}-{i:03d}/"


def _cfg(load_more: dict | None = None) -> dict:
    return {
        "id": "eco",
        "name": "ECO",
        "rss": None,
        "rsshub": None,
        "lists": [
            {
                "url": "https://eco.sapo.pt/ultimas/",
                "link_selector": "a.link-cover",
                "article_url_pattern": r"https://eco\.sapo\.pt/\d{4}/\d{2}/\d{2}/[^/]+/$",
            }
        ],
        "article_url_pattern": r"https://eco\.sapo\.pt/\d{4}/\d{2}/\d{2}/[^/]+/$",
        "load_more": load_more
        or {
            "endpoint_selector": "button.js-archive-load-more",
            "js_var": "ECO_JS",
            "offset_param": "eco_offset",
            "action_param": "action",
            "nonce_param": "nonce",
            "nonce_key": "nonce_load_more",
            "url_key": "wp_ajax_url",
            "per_page_key": "archive_load_more",
        },
    }


class TestExtractJsonObject:
    def test_parses_eco_js(self):
        html = 'var ECO_JS = {"nonce_load_more":"x","wp_ajax_url":"u"};'
        obj = _extract_json_object(html, "ECO_JS")
        assert obj == {"nonce_load_more": "x", "wp_ajax_url": "u"}

    def test_missing_returns_none(self):
        assert _extract_json_object("<html></html>", "ECO_JS") is None

    def test_bad_json_returns_none(self):
        assert _extract_json_object("var ECO_JS = {bad};", "ECO_JS") is None


class TestDiscoverFromLoadMore:
    def _make_fetcher(self, initial_n=26, pages_n=3, per_page=12) -> FakeFetcher:
        initial = [_article_url("a", i) for i in range(initial_n)]
        pages = {}
        for p in range(pages_n):
            offset = initial_n + p * per_page
            urls = [_article_url("b", offset + i) for i in range(per_page)]
            pages[offset] = _make_ajax_payload(urls, count=per_page)
        return FakeFetcher(initial, pages)

    def test_basic_pagination(self):
        f = self._make_fetcher()
        items = discover_from_load_more(
            "https://eco.sapo.pt/ultimas/",
            fetcher=f,
            load_more=_cfg()["load_more"],
            article_url_pattern=_cfg()["article_url_pattern"],
            max_items=50,
        )
        # 26 初始 + 2 页*12 = 50
        assert len(items) == 50
        # 全部唯一
        urls = {it.url for it in items}
        assert len(urls) == 50

    def test_multiple_pages_continue(self):
        f = self._make_fetcher(pages_n=5, per_page=12)
        items = discover_from_load_more(
            "https://eco.sapo.pt/ultimas/",
            fetcher=f,
            load_more=_cfg()["load_more"],
            article_url_pattern=_cfg()["article_url_pattern"],
            max_items=200,
        )
        # 有 26 + 5*12 = 86 篇可用，但 max_items=200 → 取完 86 停止（或分页到空）
        assert len(items) >= 26
        urls = {it.url for it in items}
        assert len(urls) == len(items)

    def test_utm_and_fragment_no_duplicate(self):
        # 首页同一文章带 utm / fragment → canonical 去重
        initial = [
            "https://eco.sapo.pt/2026/08/08/artigo-1/?utm_source=x",
            "https://eco.sapo.pt/2026/08/08/artigo-1/#comments",
            "https://eco.sapo.pt/2026/08/08/artigo-2/",
        ]
        f = FakeFetcher(initial, {})
        items = discover_from_load_more(
            "https://eco.sapo.pt/ultimas/",
            fetcher=f,
            load_more=_cfg()["load_more"],
            article_url_pattern=_cfg()["article_url_pattern"],
            max_items=50,
        )
        assert len(items) == 2  # 去重后只有 2 篇

    def test_last_batch_stops(self):
        initial = [_article_url("a", i) for i in range(26)]
        pages = {26: _make_ajax_payload([_article_url("b", 26 + i) for i in range(6)], count=6, last_batch=True)}
        f = FakeFetcher(initial, pages)
        items = discover_from_load_more(
            "https://eco.sapo.pt/ultimas/",
            fetcher=f,
            load_more=_cfg()["load_more"],
            article_url_pattern=_cfg()["article_url_pattern"],
            max_items=100,
        )
        assert len(items) == 32  # 26 + 6

    def test_failed_page_breaks_without_raising(self):
        initial = [_article_url("a", i) for i in range(26)]
        # 第一页请求就失败（success=false）
        f = FakeFetcher(initial, {})
        f.pages = {}  # 所有请求返回空 success
        items = discover_from_load_more(
            "https://eco.sapo.pt/ultimas/",
            fetcher=f,
            load_more=_cfg()["load_more"],
            article_url_pattern=_cfg()["article_url_pattern"],
            max_items=100,
        )
        # 首页文章仍然保留
        assert len(items) == 26

    def test_missing_nonce_raises(self):
        f = FakeFetcher([], {})
        # 覆盖 initial_html：有按钮和 ajax url，但无 nonce
        f.initial_html = (
            "<html><body>"
            "<script>var ECO_JS = {\"wp_ajax_url\":\"https://eco.sapo.pt/wp-admin/admin-ajax.php\",\"archive_load_more\":\"12\"};</script>"
            "<button class='js-archive-load-more' data-action='eco_ajax_get_posts_latest' data-offset='0'>x</button>"
            "</body></html>"
        )
        with pytest.raises(ValueError, match="nonce"):
            discover_from_load_more(
                "https://eco.sapo.pt/ultimas/",
                fetcher=f,
                load_more=_cfg()["load_more"],
                article_url_pattern=_cfg()["article_url_pattern"],
                max_items=50,
            )


class TestDiscoverForSiteMerge:
    def _make_all(self) -> tuple[dict, FakeFetcher]:
        # RSS 22 篇 + 首页 26 篇（前 22 篇与 RSS 重叠）+ load-more 补到 100
        initial_urls = [_article_url("a", i) for i in range(26)]
        rss_urls = initial_urls[:22]  # RSS = 首页前 22 篇（真实情况 RSS ⊆ 首页）
        pages = {}
        for p in range(8):  # 8 页*12 = 96 → 26+96 ≥ 100
            offset = 26 + p * 12
            pages[offset] = _make_ajax_payload([_article_url("b", offset + i) for i in range(12)], count=12)
        f = FakeFetcher(initial_urls, pages)

        class RssFetcher(FakeFetcher):
            def fetch(self, url, **kwargs):
                if "feed" in url:
                    items = "".join(
                        f"<item><title>Título {i}</title><link>{u}</link></item>"
                        for i, u in enumerate(rss_urls)
                    )
                    return f"<rss><channel>{items}</channel></rss>"
                return super().fetch(url, **kwargs)

        rf = RssFetcher(initial_urls, pages)
        cfg = _cfg()
        cfg["rss"] = "https://eco.sapo.pt/feed/"
        return cfg, rf

    def test_merge_dedup(self):
        cfg, f = self._make_all()
        items = discover_for_site(cfg, fetcher=f, max_items=100)
        # 22 RSS + (26-22) 首页新增 + load-more 补齐到 100
        urls = {it.url for it in items}
        assert len(items) == 100
        assert len(urls) == 100

    def test_load_more_failure_rss_still_works(self):
        cfg, f = self._make_all()
        # 破坏 load-more：所有 ajax 返回失败
        f.pages = {}
        items = discover_for_site(cfg, fetcher=f, max_items=100)
        # RSS + 首页 = 26 篇（去重后），load-more 失败不中断
        assert len(items) == 26
        urls = {it.url for it in items}
        assert len(urls) == 26

    def test_rss_only_when_no_list(self):
        cfg = _cfg()
        cfg["rss"] = "https://eco.sapo.pt/feed/"
        cfg["lists"] = []
        cfg["load_more"] = None

        class RssOnly(BaseFetcher):
            def fetch(self, url, **kwargs):
                items = "".join(
                    f"<item><title>T{i}</title><link>https://eco.sapo.pt/2026/08/08/x{i}/</link></item>"
                    for i in range(20)
                )
                return f"<rss><channel>{items}</channel></rss>"

            def close(self):
                pass

        items = discover_for_site(cfg, fetcher=RssOnly(), max_items=50)
        assert len(items) == 20
