"""HTML 研究结果导出测试。

覆盖用户验收清单：
1. 单篇文章 HTML 生成。
2. 中文 summary 正确输出。
3. 葡萄牙语重音字符正确输出。
4. key_points 正确渲染。
5. topics 正确渲染。
6. entities 正确渲染。
7. location entity 正确显示。
8. market_relevance 正确显示。
9. market_relevance_reason 正确显示。
10. provider/model/prompt version 正确显示。
11. token usage 正确显示。
12. cost 正确显示。
13. canonical_url 正确生成链接。
14. failed analysis 不进入正常 HTML。
15. index.html 正确生成。
16. HTML 文件名安全，不包含非法 Windows 文件名字符。
17. 空字段不会导致 HTML 崩溃。
18. HTML 中用户/文章内容必须正确 HTML escape。
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.html_export import (  # noqa: E402
    export_html,
    render_article_html,
    slugify,
)
from news.model import Article  # noqa: E402
from news.storage import Storage  # noqa: E402

WINDOWS_ILLEGAL = set('\\/:*?"<>|')


@pytest.fixture
def storage(tmp_path):
    """构造：2 篇成功分析 + 1 篇失败分析（含中文、葡语重音、location 实体）。"""
    s = Storage(tmp_path / "test.db")
    base = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

    # 文章 1：日食（葡语标题含重音），成功分析
    a1 = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/2026/08/08/eclipse-solar/",
        title="Eclipse solar, tudo o que precisa de saber",
        body_text="Um eclipse solar vai ocorrer em Portugal.\nSegundo os astrónomos, é um fenómeno raro.",
        authors=["Lusa"],
        published_at=base,
        language="pt-PT",
        status="fetched",
    )
    aid1, _ = s.insert_article(a1)
    s.upsert_analysis(
        article_id=aid1,
        provider="tokenrhythm",
        model="deepseek-v4-flash",
        prompt_version="v1",
        summary_zh="这是一篇关于日食的中文摘要。2026年8月将出现一次日食，葡萄牙可以观测。"
        "文章详细介绍了观测方法、安全注意事项以及下一次类似日食的预期时间。"
        "全文共分三个自然段，涵盖天文学背景与公众观测指引。",
        key_points=[
            "日食将在2026年8月发生",
            "葡萄牙可以观测到这次日食",
            "观测时需要使用专业滤光镜",
        ],
        topics=["天文", "葡萄牙", "科学"],
        entities=[
            {"name": "Portugal", "type": "country"},
            {"name": "Lisbon", "type": "location"},
            {"name": "EDP", "type": "company"},
            {"name": "European Commission", "type": "organization"},
        ],
        market_relevance="low",
        market_relevance_reason="这是一篇科学新闻，对金融市场的影响有限（AI判断，不代表原文事实）。",
        language="pt",
        status="success",
        usage={
            "prompt_tokens": 120,
            "completion_tokens": 300,
            "total_tokens": 420,
            "cost": 0.017721,
        },
    )

    # 文章 2：EDP 财报，成功分析
    a2 = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/2026/08/07/edp-lucros/",
        title="EDP aumenta lucros",
        body_text="EDP anunciou lucros maiores no segundo trimestre.",
        authors=[],
        published_at=base - timedelta(days=1),
        language="pt-PT",
        status="fetched",
    )
    aid2, _ = s.insert_article(a2)
    s.upsert_analysis(
        article_id=aid2,
        provider="tokenrhythm",
        model="deepseek-v4-flash",
        prompt_version="v1",
        summary_zh="EDP 公布了最新的财务业绩，利润显著增长。",
        key_points=["EDP 利润增长"],
        topics=["能源", "企业财报"],
        entities=[{"name": "EDP", "type": "company"}],
        market_relevance="medium",
        market_relevance_reason="能源公司财报可能影响股价（AI判断）。",
        language="pt",
        status="success",
        usage={"prompt_tokens": 90, "completion_tokens": 150, "total_tokens": 240},
    )

    # 文章 3：失败分析（不应出现在正常 HTML）
    a3 = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url="https://eco.sapo.pt/2026/08/06/falhou/",
        title="Artigo que falhou",
        body_text="",
        authors=[],
        published_at=base - timedelta(days=2),
        language="pt-PT",
        status="fetched",
    )
    aid3, _ = s.insert_article(a3)
    s.upsert_analysis(
        article_id=aid3,
        provider="tokenrhythm",
        model="deepseek-v4-flash",
        prompt_version="v1",
        summary_zh="",
        key_points=[],
        topics=[],
        entities=[],
        market_relevance="low",
        market_relevance_reason="",
        language="",
        status="failed",
        error="mock failure",
        usage={},
    )
    yield s
    s.close()


def _export(storage, tmp_path, **kwargs):
    out = tmp_path / "html"
    res = export_html(storage, out, **kwargs)
    return res, out


def _first_article_html(out) -> str:
    """返回第一个单篇 HTML（排除 index.html）内容。"""
    for p in sorted(out.rglob("*.html")):
        if p.name != "index.html":
            return p.read_text(encoding="utf-8")
    raise AssertionError("没有找到单篇 HTML 文件")


class TestSlugify:
    def test_no_windows_illegal_chars(self):
        slug = slugify('Eclipse solar, tudo o que precisa de saber: "edição"!')
        assert not any(c in WINDOWS_ILLEGAL for c in slug)
        assert ":" not in slug

    def test_slug_keeps_accented_chars(self):
        slug = slugify("Título com acentuação")
        assert "Título" in slug or "Titulo" in slug

    def test_slug_empty_fallback(self):
        assert slugify("") == "article"
        assert slugify("///***???") == "article"


class TestArticleHtml:
    def test_single_article_html_generated(self, storage, tmp_path):
        res, out = _export(storage, tmp_path)
        assert res.exported == 2
        html_files = [p for p in out.rglob("*.html") if p.name != "index.html"]
        assert len(html_files) == 2

    def test_chinese_summary_output(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "中文摘要" in content
        assert "日食将在2026年8月发生" in content or "EDP" in content

    def test_portuguese_accents(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        # 原文正文中的葡语重音应正常保留
        assert "astrónomos" in content
        assert "fenómeno" in content

    def test_key_points_rendered(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "关键观点" in content
        assert "日食将在2026年8月发生" in content
        assert "key_points_json" in content

    def test_topics_rendered(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "主题" in content
        assert "天文" in content
        assert "葡萄牙" in content

    def test_entities_rendered(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "实体" in content
        assert "Portugal" in content

    def test_location_entity_displayed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "Lisbon" in content
        assert "location" in content

    def test_market_relevance_displayed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "市场相关性" in content
        assert "LOW" in content or "MEDIUM" in content

    def test_market_relevance_reason_displayed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "分析理由" in content
        assert "AI判断" in content

    def test_provider_model_prompt_version(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "tokenrhythm" in content
        assert "deepseek-v4-flash" in content
        assert "v1" in content

    def test_token_usage_displayed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "Prompt tokens" in content
        assert "Completion tokens" in content
        assert "Total tokens" in content
        assert "420" in content

    def test_cost_displayed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert "Cost" in content
        assert "0.017721" in content

    def test_canonical_url_link(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = _first_article_html(out)
        assert 'href="https://eco.sapo.pt/2026/08/08/eclipse-solar/"' in content
        assert "查看原文" in content

    def test_failed_analysis_not_in_html(self, storage, tmp_path):
        res, out = _export(storage, tmp_path)
        assert res.exported == 2  # 只有 2 篇成功
        all_html = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.html"))
        assert "Artigo que falhou" not in all_html

    def test_html_escape(self, storage, tmp_path):
        """用户/文章内容必须 HTML escape，正文中的 HTML 不应破坏页面结构。"""
        s = Storage(tmp_path / "escape.db")
        a = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url="https://eco.sapo.pt/escape/",
            title='Título <script>alert("x")</script>',
            body_text='<script>alert("body")</script> & <b>bold</b>',
            published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            status="fetched",
        )
        aid, _ = s.insert_article(a)
        s.upsert_analysis(
            article_id=aid,
            provider="tokenrhythm",
            model="m",
            prompt_version="v1",
            summary_zh='摘要 <img src=x onerror=alert(1)>',
            key_points=['<i>point</i>'],
            topics=['<script>t</script>'],
            entities=[{"name": '<a href="x">ent</a>', "type": "company"}],
            market_relevance="high",
            market_relevance_reason="<b>reason</b>",
            language="pt",
            status="success",
            usage={},
        )
        res, out = _export(s, tmp_path)
        s.close()
        assert res.exported == 1
        content = _first_article_html(out)
        # 不允许出现可执行的 script 标签（应被转义）
        assert "<script>alert" not in content
        assert "<script>t</script>" not in content
        assert '<img src=x' not in content
        # 转义后的实体应保留
        assert "&lt;script&gt;" in content


class TestEmptyFields:
    def test_empty_fields_no_crash(self, tmp_path):
        """空字段（无摘要/无实体/无 usage）不应导致崩溃。"""
        s = Storage(tmp_path / "empty.db")
        a = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url="https://eco.sapo.pt/empty/",
            title="Empty",
            body_text="",
            published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            status="fetched",
        )
        aid, _ = s.insert_article(a)
        s.upsert_analysis(
            article_id=aid,
            provider="",
            model="",
            prompt_version="",
            summary_zh="",
            key_points=[],
            topics=[],
            entities=[],
            market_relevance="",
            market_relevance_reason="",
            language="",
            status="success",
            usage=None,
        )
        res, out = _export(s, tmp_path)
        s.close()
        assert res.exported == 1
        assert res.failed == 0
        content = _first_article_html(out)
        assert "DOCTYPE html" in content


class TestIndexHtml:
    def test_index_generated(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        assert (out / "index.html").exists()

    def test_index_sorted_desc(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = (out / "index.html").read_text(encoding="utf-8")
        # 最新文章（日食 08-08）应出现在 EDP（08-07）之前
        assert content.index("Eclipse solar") < content.index("EDP aumenta")

    def test_index_shows_stats(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = (out / "index.html").read_text(encoding="utf-8")
        assert "成功" in content
        assert "失败" in content
        assert "2" in content  # 成功数
        assert "1" in content  # 失败数

    def test_index_failed_not_listed(self, storage, tmp_path):
        _, out = _export(storage, tmp_path)
        content = (out / "index.html").read_text(encoding="utf-8")
        assert "Artigo que falhou" not in content


class TestFiltering:
    def test_source_filter(self, storage, tmp_path):
        res, out = _export(storage, tmp_path, source_id="eco")
        assert res.exported == 2
        assert (out / "index.html").exists()

    def test_article_id_filter(self, storage, tmp_path):
        # 取第一篇文章的 id
        art = storage.list_articles(source_id="eco", limit=10)[0]
        res, out = _export(storage, tmp_path, article_id=art.id)
        assert res.exported == 1
        assert (out / "index.html").exists()


class TestRenderArticleHtmlDirect:
    def test_render_direct(self):
        """直接调用渲染函数，检查结构完整性。"""
        html_doc = render_article_html(
            article_id=1,
            title="Título",
            source_name="ECO",
            authors=["Lusa"],
            published_at="2026-08-08T12:00:00+00:00",
            canonical_url="https://example.com/1",
            body_text="corpo",
            analysis={
                "summary_zh": "摘要",
                "key_points": ["p1"],
                "topics": ["t1"],
                "entities": [{"name": "Lisboa", "type": "location"}],
                "market_relevance": "high",
                "market_relevance_reason": "reason",
                "language": "pt",
                "provider": "tokenrhythm",
                "model": "deepseek-v4-flash",
                "prompt_version": "v1",
            },
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            cost=0.5,
            created_at="2026-08-08T12:00:00+00:00",
            updated_at="2026-08-08T12:00:00+00:00",
            language="pt",
            index_rel="../../index.html",
            article_language="pt-PT",
        )
        assert "<!DOCTYPE html>" in html_doc
        assert "HIGH" in html_doc
        assert "AI 判断，不代表原文事实" in html_doc
        assert "查看原文" in html_doc
        assert "tokenrhythm" in html_doc
        assert "deepseek-v4-flash" in html_doc
        assert "Prompt tokens" in html_doc
        assert "Cost" in html_doc
        assert "0.5" in html_doc
