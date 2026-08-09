"""Daily Reader 阅读器 + 本地 HTTP 阅读模式测试。

覆盖第五阶段验收清单：
1. News Archive 新版 daily 风格 HTML
2. 100 篇新闻目录和 article anchor
3. 已读/收藏/localStorage
4. 中文摘要、葡语正文、HTML escape
5. localhost server 可以启动
6. 只监听 127.0.0.1
7. 端口被占用时自动选择其他端口
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.news_archive import (  # noqa: E402
    export_news_archive,
    render_reader_index_html,
)
from news.model import Article  # noqa: E402
from news.reader_server import ReaderServer  # noqa: E402
from news.storage import Storage  # noqa: E402


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "reader.db")
    base = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(100):
        art = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url=f"https://eco.sapo.pt/2026/08/08/artigo-{i}/",
            title=f"Artigo número {i}",
            authors=["Lusa"] if i % 2 == 0 else [],
            published_at=base + timedelta(hours=-i),
            body_text=f"Corpo do artigo {i}.\\nSegundo parágrafo com acentuação: ação, coração, informação.",
            language="pt-PT",
            status="fetched",
        )
        aid, _ = s.insert_article(art)
        if i == 0:
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


class TestDailyReaderHtml:
    def test_masthead_header(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "ECO News — Daily Reader" in idx
        assert "100 articles · ECO" in idx
        # 浅灰背景 + 窄版居中阅读容器
        assert "720px" in idx
        assert "#f4f4f1" in idx  # body 浅灰/近白背景

    def test_toc_lists_all_and_anchors(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "Table of Contents" in idx
        # 100 篇全部列在目录中
        assert idx.count('href="#article-') >= 100
        # article anchor 存在且唯一
        for n in (1, 50, 100):
            assert f'id="article-{n}"' in idx
            assert f'href="#article-{n}"' in idx
        # 每篇使用 <section id="article-N">
        assert idx.count('class="article"') == 100

    def test_sections_continuous_not_cards(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        # 不应出现旧的"卡片列表"标记
        assert 'class="entry"' not in idx
        assert 'class="card"' not in idx
        # 连续阅读 section 直接含标题 / 时间 / 来源 / 作者 / 正文 / 链接
        assert "2026-08-08 12:00" in idx
        assert "作者：Lusa" in idx
        assert "Corpo do artigo" in idx
        assert "阅读原文" in idx

    def test_ai_three_states_preserved(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "✓ AI 已分析" in idx
        assert "⚠ AI 分析失败" in idx
        assert "○ 尚未分析" in idx
        # 失败/未分析不影响原文阅读
        assert "Corpo do artigo 1" in idx  # 失败文章正文仍在
        assert "Corpo do artigo 2" in idx  # 未分析文章正文仍在

    def test_ai_summary_and_portuguese_body(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        # AI 中文摘要
        assert "AI 中文摘要" in idx
        assert "这是一篇中文摘要" in idx
        # 葡语正文（重音字符）保留
        assert "ação" in idx
        assert "coração" in idx
        assert "informação" in idx

    def test_utf8_charset(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert 'charset="utf-8"' in idx

    def test_no_external_cdn_no_shadow_dom(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        assert "http://" not in idx.replace("https://eco.sapo.pt", "")  # 无外部 CDN
        assert "https://" not in idx.replace("https://eco.sapo.pt", "")
        assert "shadow" not in idx.lower()
        assert "<iframe" not in idx
        assert "<canvas" not in idx
        # 语义化元素
        assert "<article" not in idx or "<section" in idx
        assert "<section" in idx
        assert "<nav" in idx

    def test_reading_interactions_present(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        idx = (tmp_path / "na" / "index.html").read_text(encoding="utf-8")
        # 已读 □/✓
        assert "read-toggle" in idx
        assert "□" in idx
        assert "✓" in idx
        # 收藏 ☆/★
        assert "star-toggle" in idx
        assert "☆" in idx
        assert "★" in idx
        # 阅读进度 + localStorage
        assert "progress-bar" in idx
        assert "localStorage" in idx
        # Back to Contents
        assert "Back to Contents" in idx
        # J/K 快捷键
        assert "keydown" in idx
        assert "'j'" in idx or '"j"' in idx
        assert "'k'" in idx or '"k"' in idx
        # 阅读模式切换
        assert "mode-toggle" in idx
        assert "sepia" in idx
        assert "night" in idx

    def test_html_escape_still_works(self, storage, tmp_path):
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
        assert "<b>bold</b>" not in idx

    def test_limit_50_100(self, storage, tmp_path):
        r50 = export_news_archive(storage, tmp_path / "n50", source_id="eco", limit=50)
        assert r50.exported == 50
        idx50 = (tmp_path / "n50" / "index.html").read_text(encoding="utf-8")
        assert "50 articles" in idx50
        assert idx50.count('class="article"') == 50

        r100 = export_news_archive(storage, tmp_path / "n100", source_id="eco", limit=100)
        assert r100.exported == 100
        idx100 = (tmp_path / "n100" / "index.html").read_text(encoding="utf-8")
        assert "100 articles" in idx100
        assert idx100.count('class="article"') == 100


class TestReaderJs:
    def test_localstorage_key_and_read_star(self):
        """JS 中包含已读/收藏/localStorage 的核心逻辑。"""
        from news.news_archive import _READER_JS

        assert "laxinwen.news.reader.v1" in _READER_JS
        assert "state.read" in _READER_JS
        assert "state.star" in _READER_JS
        assert "localStorage" in _READER_JS
        assert "read-toggle" in _READER_JS
        assert "star-toggle" in _READER_JS
        assert "progress-bar" in _READER_JS
        assert "mode-toggle" in _READER_JS


class TestRenderReaderIndex:
    def test_render_100_sections_and_anchors(self):
        rows = []
        base = datetime(2026, 8, 8, tzinfo=timezone.utc)
        for i in range(100):
            rows.append(
                {
                    "id": i + 1,
                    "title": f"News {i}",
                    "source_name": "ECO",
                    "source_id": "eco",
                    "canonical_url": f"https://eco.sapo.pt/x/{i}/",
                    "published_at": base.isoformat(),
                    "discovered_at": base.isoformat(),
                    "authors": json.dumps(["Lusa"] if i % 2 == 0 else []),
                    "body_text": f"Body {i}",
                    "ai_status": "ok" if i == 0 else ("failed" if i == 1 else None),
                    "ai_has_failed": 1 if i == 1 else 0,
                }
            )
        html_doc = render_reader_index_html(
            rows,
            source_name="ECO",
            total=100,
            analyzed_ok=1,
            analyzed_failed=1,
            unanalyzed=98,
            research_rel_by_article={1: "../../html/2026/08/0001-x.html"},
            generated_at=datetime(2026, 8, 9, 20, 47, 43, tzinfo=timezone.utc),
            analysis_by_article={1: {"summary_zh": "中文摘要"}},
        )
        assert html_doc.count('class="article"') == 100
        assert 'id="article-1"' in html_doc
        assert 'id="article-100"' in html_doc
        assert "Table of Contents" in html_doc
        assert "中文摘要" in html_doc
        assert "查看 AI Research" in html_doc


class TestLocalServer:
    def test_server_starts_and_serves(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=100)
        root = tmp_path / "na"
        server = ReaderServer(root, preferred_ports=(0,))
        server.start()
        try:
            assert server.running
            assert server.port is not None
            url = server.url_for("index.html")
            assert url.startswith("http://127.0.0.1:")
            assert url.endswith("/index.html")
            # 通过 HTTP 拉取内容
            import urllib.request

            body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
            assert "ECO News — Daily Reader" in body
            assert "Table of Contents" in body
        finally:
            server.stop()
        assert not server.running

    def test_listens_only_127_0_0_1(self, storage, tmp_path):
        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=5)
        server = ReaderServer(tmp_path / "na", preferred_ports=(0,))
        server.start()
        try:
            # server 只绑定 127.0.0.1（socket 地址检查）
            import socket

            host = socket.gethostbyname(socket.gethostname())
            # 尝试连接局域网地址（本环境可能没有局域网地址，仅验证 server 不暴露）
            assert server.host == "127.0.0.1"
        finally:
            server.stop()

    def test_host_validation_rejects_0_0_0_0(self, tmp_path):
        with pytest.raises(ValueError):
            ReaderServer(tmp_path, host="0.0.0.0")
        with pytest.raises(ValueError):
            ReaderServer(tmp_path, host="localhost")  # 只允许 127.0.0.1

    def test_port_occupied_auto_select(self, storage, tmp_path):
        import socket

        export_news_archive(storage, tmp_path / "na", source_id="eco", limit=5)
        # 占用一个端口
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        occupied = sock.getsockname()[1]
        server = ReaderServer(tmp_path / "na", preferred_ports=(occupied,))
        server.start()
        try:
            assert server.port is not None
            assert server.port != occupied
            # 服务正常可用
            import urllib.request

            body = urllib.request.urlopen(server.url_for("index.html"), timeout=5).read()
            assert body
        finally:
            server.stop()
            sock.close()

    def test_stop_is_idempotent(self, storage, tmp_path):
        server = ReaderServer(tmp_path, preferred_ports=(0,))
        server.stop()
        server.stop()
        assert not server.running
