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
