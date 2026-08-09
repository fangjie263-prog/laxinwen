"""端到端 pipeline 测试。

- 使用 FakeFetcher 提供离线 HTML，验证 发现→去重→下载→提取→入库 全流程；
- 验证重复执行不重复插入（URL 去重 / 标题指纹去重）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.fetch import BaseFetcher  # noqa: E402
from news.pipeline import Pipeline  # noqa: E402
from news.storage import Storage  # noqa: E402

_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="pt">
<head>
  <title>Notícia Teste - ECO</title>
  <meta property="article:published_time" content="2026-08-08T10:00:00+00:00"/>
  <link rel="canonical" href="https://eco.sapo.pt/2026/08/08/noticia-teste/"/>
</head>
<body>
  <article>
    <h1>Notícia Teste</h1>
    <p class="author">Por <a>Maria Silva</a></p>
    <p>Primeiro parágrafo do corpo da notícia.</p>
    <p>Segundo parágrafo com mais conteúdo para o teste.</p>
  </article>
</body>
</html>
"""


class FakeFetcher(BaseFetcher):
    """离线假抓取器。"""

    def __init__(self, html: str):
        self.html = html
        self.calls: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        return self.html

    def close(self) -> None:
        pass


class FakeStorage(Storage):
    """用临时文件建库的辅助。"""

    def __init__(self, tmp_path):
        super().__init__(tmp_path / "e2e.db")


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "e2e.db")
    yield s
    s.close()


class TestPipeline:
    def test_single_run_inserts(self, storage):
        pipe = Pipeline(storage, fetcher=FakeFetcher(_ARTICLE_HTML))
        # 直接注入一篇发现条目，模拟 discover 结果
        from news.discover import DiscoveredItem

        items = [
            DiscoveredItem(url="https://eco.sapo.pt/2026/08/08/noticia-teste/")
        ]
        stats = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        assert stats.fetched_ok == 1
        assert storage.count() == 1
        art = storage.list_articles(limit=1)[0]
        assert "Primeiro parágrafo" in art.body_text
        pipe.close()

    def test_second_run_dedup_by_url(self, storage):
        pipe = Pipeline(storage, fetcher=FakeFetcher(_ARTICLE_HTML))
        from news.discover import DiscoveredItem

        items = [DiscoveredItem(url="https://eco.sapo.pt/2026/08/08/noticia-teste/")]
        stats1 = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        stats2 = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        assert stats1.fetched_ok == 1
        assert stats2.skipped_dup == 1
        assert storage.count() == 1
        pipe.close()

    def test_single_failure_does_not_break(self, storage):
        class FailingFetcher(FakeFetcher):
            def fetch(self, url, **kwargs):
                if "bad" in url:
                    raise RuntimeError("connection refused")
                return super().fetch(url, **kwargs)

        pipe = Pipeline(storage, fetcher=FailingFetcher(_ARTICLE_HTML))
        from news.discover import DiscoveredItem

        items = [
            DiscoveredItem(url="https://eco.sapo.pt/2026/08/08/good-one/"),
            DiscoveredItem(url="https://eco.sapo.pt/2026/08/08/bad-one/"),
            DiscoveredItem(url="https://eco.sapo.pt/2026/08/08/good-two/"),
        ]
        stats = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        # 失败一篇不影响其它
        assert stats.fetched_ok == 2
        assert stats.failed == 1
        assert storage.count() == 3
        bad = [a for a in storage.list_articles(limit=10) if "bad-one" in a.canonical_url][0]
        assert bad.status == "failed"
        pipe.close()


# ---------- 第三阶段：load-more 发现 + 去重集成测试 ----------


def _card(url: str) -> str:
    return (
        f'<article class="card-list card card--list">'
        f'<a class="link-cover" href="{url}"></a>'
        f'<h3 class="card__title"><a href="{url}">Título {url[-10:]}</a></h3>'
        f"</article>"
    )


class LoadMoreFetcher(BaseFetcher):
    """模拟 ECO：RSS + 首页 + admin-ajax 分页。"""

    def __init__(self, initial_n=26, pages_n=8):
        self.initial = [f"https://eco.sapo.pt/2026/08/08/inicial-{i}/" for i in range(initial_n)]
        self.pages = {}
        for p in range(pages_n):
            offset = initial_n + p * 12
            self.pages[offset] = {
                "success": True,
                "data": {
                    "posts_html": "".join(
                        _card(f"https://eco.sapo.pt/2026/08/08/pagina-{offset + i}/")
                        for i in range(12)
                    ),
                    "count": 12,
                },
            }
        self.initial_html = (
            "<html><head><script>var ECO_JS = "
            '{"nonce_load_more":"abc","wp_ajax_url":"https://eco.sapo.pt/wp-admin/admin-ajax.php","archive_load_more":"12"};'
            "</script></head><body>"
            + "".join(_card(u) for u in self.initial)
            + '<button class="js-archive-load-more" data-action="eco_ajax_get_posts_latest" data-offset="26">x</button>'
            + "</body></html>"
        )
        self.calls: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        if "admin-ajax" in url:
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(url).query)
            offset = int(qs.get("eco_offset", ["0"])[0])
            if offset in self.pages:
                import json

                return json.dumps(self.pages[offset])
            return '{"success":true,"data":{"posts_html":"","count":0}}'
        return self.initial_html

    def close(self) -> None:
        pass


class TestPipelineLoadMore:
    def test_fetch_limit_100_then_rerun_no_duplicate(self, storage):
        """多次 fetch 不重复插入（RSS + load-more 合并后去重）。"""
        fetcher = LoadMoreFetcher()
        pipe = Pipeline(storage, fetcher=fetcher, max_items=100)
        # 用 run_site 走完整流程需要站点配置；这里直接验证 discover + ingest
        from news.config import load_site_config

        cfg = load_site_config("eco")
        # 覆盖 fetcher
        from news.discover import discover_for_site

        items = discover_for_site(cfg, fetcher=fetcher, max_items=100)
        assert len(items) == 100
        stats1 = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        assert stats1.fetched_ok == 100
        assert storage.count() == 100

        # 第二次：同样的 discover → 全部去重
        items2 = discover_for_site(cfg, fetcher=fetcher, max_items=100)
        stats2 = pipe._ingest_items(items2, "eco", "ECO", "pt-PT", {}, [])
        assert stats2.skipped_dup == 100
        assert stats2.fetched_ok == 0
        assert storage.count() == 100
        pipe.close()

    def test_load_more_failure_falls_back_to_rss_list(self, storage):
        """load-more 失败时 RSS/栏目页仍然可以入库。"""
        fetcher = LoadMoreFetcher()
        # 破坏 admin-ajax：全部返回 success=false
        fetcher.pages = {}
        from news.config import load_site_config
        from news.discover import discover_for_site

        cfg = load_site_config("eco")
        items = discover_for_site(cfg, fetcher=fetcher, max_items=100)
        # RSS 22 篇 + 首页 26 篇（有重叠）→ 至少 26 篇
        assert len(items) >= 26
        pipe = Pipeline(storage, fetcher=fetcher, max_items=100)
        stats = pipe._ingest_items(items, "eco", "ECO", "pt-PT", {}, [])
        assert stats.fetched_ok == len(items)
        assert storage.count() == len(items)
        pipe.close()
