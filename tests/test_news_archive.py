"""News Archive HTML 导出测试。

覆盖用户验收清单：
- News Archive 不要求 AI 成功（未分析/失败文章也显示）
- AI 成功文章显示 Research 链接
- AI 失败文章仍显示在 News Archive（⚠ 状态）
- 未分析文章仍显示（○ 状态）
- news-html --limit 50 只显示最近 50 篇
- news-html --limit 100 只显示最近 100 篇
- 原来的 news export --format html 继续正常工作
- 单篇页有 AI → 显示 AI 详情；无 AI → 显示"尚未进行 AI 分析"；失败 → 显示失败提示
- HTML escape
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.news_archive import (  # noqa: E402
    export_news_archive,
    render_article_page,
    slugify,
)
from news.html_export import export_html  # noqa: E402
from news.model import Article  # noqa: E402
from news.storage import Storage  # noqa: E402


@pytest.fixture
def storage(tmp_path):
    """构造：100 篇文章（1 成功分析 + 1 失败分析 + 98 未分析）。"""
    s = Storage(tmp_path / "test.db")
    base = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(100):
        art = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url=f"https://eco.sapo.pt/2026/08/0{8}/artigo-{i}/",
            title=f"Artigo número {i}",
            authors=["Lusa"] if i % 2 == 0 else [],
            published_at=base + timedelta(hours=-i),
            body_text=f"Corpo do artigo {i}.\\nSegundo parágrafo.",
            language="pt-PT",
            status="fetched",
        )
        aid, _ = s.insert_article(art)
        if i == 0:
            # 成功分析
            s.upsert_analysis(
                article_id=aid,
                provider="tokenrhythm",
                model="deepseek-v4-flash",
                prompt_version="v1",
                summary_zh="这是一篇中文摘要，关于葡萄牙经济的最新动态。",
                key_points=["葡萄牙经济有变化", "市场关注"],
                topics=["葡萄牙", "经济"],
                entities=[{"name": "Portugal", "type": "country"}],
                market_relevance="medium",
                market_relevance_reason="涉及宏观经济（AI判断）。",
                language="pt",
                status="success",
            )
        elif i == 1:
            # 失败分析
            s.upsert_analysis(
                article_id=aid,
                provider="tokenrhythm",
                model="deepseek-v4-flash",
                prompt_version="v1",
                summary_zh="",
                key_points=[],
                topics=[],
                entities=[],
                market_relevance="",
                market_relevance_reason="",
                language="",
                status="failed",
                error="API timeout",
            )
    yield s
    s.close()


class TestNewsArchiveExport:
    def test_exports_all_statuses(self, storage, tmp_path):
        res = export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        assert res.exported == 100
        assert res.analyzed_ok == 1
        assert res.analyzed_failed == 1
        assert res.unanalyzed == 98
        assert res.index_path.exists()

    def test_limit_50(self, storage, tmp_path):
        res = export_news_archive(storage, tmp_path / "na50", source_id="eco", limit=50)
        assert res.exported == 50
        idx = (tmp_path / "na50" / "index.html").read_text(encoding="utf-8")
        assert "最近 50 条" in idx
        assert "最近 100 条" not in idx

    def test_limit_100(self, storage, tmp_path):
        res = export_news_archive(storage, tmp_path / "na100", source_id="eco", limit=100)
        assert res.exported == 100
        idx = (tmp_path / "na100" / "index.html").read_text(encoding="utf-8")
        assert "最近 100 条" in idx

    def test_index_has_all_ai_status_badges(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "✓ AI 已分析" in idx
        assert "⚠ AI 分析失败" in idx
        assert "○ 尚未分析" in idx

    def test_index_has_summary_for_analyzed(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        # Daily Reader：已分析文章的 AI 中文摘要直接显示在对应 article section 中
        assert "AI 中文摘要" in idx
        assert "这是一篇中文摘要" in idx
        # 未分析文章不显示摘要文本
        assert "Corpo do artigo" in idx
        # 100 篇全部列出，不截断成摘要卡片
        assert idx.count('class="article"') == 100

    def test_single_pages_created(self, storage, tmp_path):
        res = export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        html_files = list((tmp_path / "na").rglob("*.html"))
        # index.html + 100 单篇 = 101
        assert len(html_files) == 101

    def test_analyzed_single_has_ai_details(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        # 找第一篇（成功分析）
        files = list((tmp_path / "na" / "2026" / "08").glob("0000-*.html"))
        if not files:
            files = list((tmp_path / "na").rglob("*.html"))
        # 找到含"关键观点"的单篇页
        for f in (tmp_path / "na").rglob("*.html"):
            html = f.read_text(encoding="utf-8")
            if "关键观点" in html:
                assert "中文摘要" in html
                assert "市场相关性" in html
                assert "主题" in html
                return
        pytest.fail("未找到含 AI 详情的单篇页")

    def test_failed_single_shows_failed(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        found = False
        for f in (tmp_path / "na").rglob("*.html"):
            html = f.read_text(encoding="utf-8")
            if "⚠ AI 分析失败" in html:
                found = True
                # 失败文章仍然保留原文正文（不影响阅读）
                assert "原文正文" in html or "Corpo do artigo" in html
                break
        assert found
        # 失败文章在 index 中也保留（连续阅读 section 中显示失败状态）
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "⚠ AI 分析失败" in idx

    def test_unanalyzed_single_shows_pending(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        found = False
        for f in (tmp_path / "na").rglob("*.html"):
            html = f.read_text(encoding="utf-8")
            if "尚未进行 AI 分析" in html:
                found = True
                break
        assert found

    def test_original_body_present(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=5)
        for f in (tmp_path / "na").rglob("*.html"):
            html = f.read_text(encoding="utf-8")
            if "Corpo do artigo" in html:
                return
        pytest.fail("单篇页未包含原文正文")

    def test_html_escape(self, storage, tmp_path):
        # 注入带 HTML 的文章标题
        art = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url="https://eco.sapo.pt/2026/08/08/escape-test/",
            title='<script>alert("xss")</script> & "quote"',
            body_text="<b>bold</b> & <i>italic</i>",
            published_at=datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc),
            language="pt-PT",
            status="fetched",
        )
        storage.insert_article(art)
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=200)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "<script>alert" not in idx
        assert "&lt;script&gt;" in idx

    def test_research_html_still_works(self, storage, tmp_path):
        # 原有 AI Research HTML 继续正常工作（只显示成功分析）
        res = export_html(storage, tmp_path / "html", source_id="eco")
        assert res.exported == 1  # 只有 1 篇成功
        assert res.analysis_ok == 1
        assert res.analysis_failed == 1

    def test_research_link_in_archive_when_html_exists(self, storage, tmp_path):
        # 先生成 AI Research，再生成 News Archive → index 应有 Research 链接
        export_html(storage, tmp_path / "html", source_id="eco")
        res = export_news_archive(storage, tmp_path / "news-html" / "eco", source_id="eco", limit=100)
        # research_root 定位到 tmp_path/html
        idx = (tmp_path / "news-html" / "eco" / "index.html").read_text(encoding="utf-8")
        assert "AI Research" in idx


class TestRenderArticlePage:
    def test_analyzed_renders_full_ai(self):
        html = render_article_page(
            article_id=1,
            title="Título",
            source_name="ECO",
            authors=["Lusa"],
            published_at="2026-08-08T10:00:00+00:00",
            canonical_url="https://eco.sapo.pt/2026/08/08/x/",
            body_text="Corpo",
            ai_status="ok",
            analysis={
                "summary_zh": "摘要",
                "key_points": ["点1", "点2"],
                "topics": ["主题A"],
                "entities": [{"name": "EDP", "type": "company"}],
                "market_relevance": "high",
                "market_relevance_reason": "原因",
                "language": "pt",
            },
        )
        assert "✓ AI 已分析" in html
        assert "摘要" in html
        assert "点1" in html
        assert "主题A" in html
        assert "EDP" in html
        assert "HIGH" in html
        assert "尚未进行 AI 分析" not in html

    def test_failed_renders_failed_status(self):
        html = render_article_page(
            article_id=2,
            title="Título",
            source_name="ECO",
            authors=[],
            published_at=None,
            canonical_url="https://eco.sapo.pt/x/",
            body_text="",
            ai_status="failed",
            analysis={},
        )
        assert "⚠ AI 分析失败" in html
        assert "AI 分析" in html

    def test_none_renders_pending(self):
        html = render_article_page(
            article_id=3,
            title="Título",
            source_name="ECO",
            authors=[],
            published_at=None,
            canonical_url="https://eco.sapo.pt/x/",
            body_text="Corpo",
            ai_status="none",
            analysis={},
        )
        assert "○ 尚未分析" in html
        assert "尚未进行 AI 分析" in html
        assert "Corpo" in html


class TestSlugify:
    def test_portuguese_accents(self):
        s = slugify("Revitalização da Serra da Estrela é promessa")
        assert "Revitaliza" in s
        assert " " not in s

    def test_illegal_windows_chars_removed(self):
        assert "<" not in slugify("a<b>c:d")
        assert ":" not in slugify("a<b>c:d")
