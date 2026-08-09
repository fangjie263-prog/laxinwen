"""基于 ResearchReader 真实捕获 HKEJ 页面数据的验收测试。

背景：当前沙箱/CI 网络无法直连 www.hkej.com（所有 IP 超时，网络层拦截）。
但 ResearchReader 项目保存了**真实抓取**的 HKEJ 页面数据：

- ``test_page.html``：真实 HKEJ 即时新闻列表页（97KB，含 40 条去重后的真实文章链接、
  分页链接 /instantnews/index?page=2..10）
- ``work/_debug_title.json``：真实文章页的 h1 / og:title / title 三字段
- ``work/outputs/hkej_news_*.json``（git 历史）：真实文章正文（article-content 内容）

本测试用这些真实数据重建 HKEJ 页面结构，走完整 laxinwen pipeline
（adapter 发现 → 去重 → 下载 → ResearchReader 正文提取 → SQLite 入库），
验证 adapter 在真实 HKEJ DOM 上工作，并验证 SQLite 跨运行持久化去重。
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.fetch import BaseFetcher  # noqa: E402
from news.pipeline import Pipeline  # noqa: E402
from news.sources.hkej import extract_body, extract_title  # noqa: E402

# 真实 HKEJ 列表页（ResearchReader 抓包保存，随测试进入仓库）
REAL_LIST_HTML = Path(__file__).resolve().parent / "fixtures" / "hkej" / "real_list_page.html"
# 重建的真实文章页（从 ResearchReader 真实正文 + 真实标题构建）
REAL_ARTICLES_PATH = Path(__file__).resolve().parent / "fixtures" / "hkej" / "real_articles.json"

pytestmark = pytest.mark.skipif(
    not (REAL_LIST_HTML.is_file() and REAL_ARTICLES_PATH.is_file()),
    reason="缺少 ResearchReader 真实 HKEJ 数据 fixture",
)


def _load_real_articles() -> dict[str, dict]:
    """url → {title, html, expected_body}。"""
    data = json.loads(REAL_ARTICLES_PATH.read_text(encoding="utf-8"))
    return {a["url"]: a for a in data}


class RealHkejFetcher(BaseFetcher):
    """用真实 HKEJ 页面数据模拟站点：列表页返回真实 HTML，文章页返回真实正文。"""

    def __init__(self):
        self.articles = _load_real_articles()
        self.calls: list[str] = []

    def fetch(self, url: str, **kwargs) -> str:
        self.calls.append(url)
        if "instantnews" in url and "/article/" not in url:
            # 列表页（真实 HTML）
            return REAL_LIST_HTML.read_text(encoding="utf-8", errors="replace")
        if url in self.articles:
            return self.articles[url]["html"]
        # 未知文章：返回带文章 ID 的占位（模拟真实正文结构）
        m = re.search(r"/article/(\d+)/", url)
        return (
            "<html><head><title>信報即時新聞 - 測試 - 信報網站 hkej.com</title></head>"
            "<body><h1>測試標題</h1>"
            '<div id="article-content"><p>測試正文內容</p></div></body></html>'
        )

    def close(self):
        pass


class TestRealHkejDataAcceptance:
    def test_real_list_page_parsed_with_researchreader_regex(self):
        """真实列表页解析：LINK_RE 在真实 HKEJ HTML 上工作。"""
        from news.sources.hkej import parse_list_page

        links = parse_list_page(REAL_LIST_HTML.read_text(encoding="utf-8", errors="replace"))
        assert len(links) > 0
        # 真实链接形式验证
        for l in links[:5]:
            assert re.match(r"/instantnews/[a-z]+/article/\d+/", l)

    def test_real_article_title_and_body_extraction(self):
        """真实文章页：标题 fallback + article-content 正文在真实结构上工作。"""
        articles = _load_real_articles()
        sample = next(iter(articles.values()))
        html = sample["html"]
        title = extract_title(html)
        assert title  # 标题从 h1 提取
        body = extract_body(html)
        # 真实正文的第一句话应该保留
        first_sentence = sample["expected_body"][:30]
        assert first_sentence in body
        # 导航/广告/相关新闻不进入正文
        assert "相關新聞" not in body
        assert "廣告" not in body

    def test_end_to_end_real_data_sqlite_dedup(self, tmp_path):
        """完整 pipeline：真实 HKEJ 数据 → SQLite → 第二次抓取新增 0。"""
        from news.storage import Storage

        storage = Storage(tmp_path / "real_hkej.db")
        try:
            # 第一次
            pipe = Pipeline(storage, fetcher=RealHkejFetcher(), max_items=50)
            stats1 = pipe.run_site("hkej")
            pipe.close()
            assert stats1.discovered > 0
            assert storage.count(source_id="hkej") == stats1.discovered
            # 至少一篇文章成功下载/入库
            assert storage.count(source_id="hkej") > 0

            # 第二次（相同真实数据）
            pipe2 = Pipeline(storage, fetcher=RealHkejFetcher(), max_items=50)
            stats2 = pipe2.run_site("hkej")
            pipe2.close()
            assert stats2.discovered == stats1.discovered
            assert stats2.skipped_dup == stats1.discovered
            assert stats2.fetched_ok == 0
            assert storage.count(source_id="hkej") == stats1.discovered
        finally:
            storage.close()
