"""便携式 HTML 导出测试（独立 HTML / HTML 新闻包）。

覆盖验收清单：
- 独立 HTML 是真正 self-contained（CSS/JS 内嵌，无外部 CDN / localhost 依赖）
- 双击 index.html 或单个 .html 即可阅读（无需 laxinwen / Python / 服务器）
- 独立 HTML 保留 Daily Reader 功能（标题/日期/数量/TOC/正文/已读/收藏/进度/Day-Sepia-Night/J-K/Back to Contents）
- AI 摘要 + Research 链接 + 原文链接 + AI 状态
- HTML 新闻包输出 data/export/portable/<site>-<date>/，含 index.html + articles/NNN.html
- 所有展示时间统一北京时间（Asia/Shanghai）24 小时制
"""

import sys
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.model import Article  # noqa: E402
from news.portable import (  # noqa: E402
    _OPEN_READER_BAT,
    _PORTABLE_SERVER_PY,
    export_independent_html,
    export_portable_package,
    export_portable_reader_package,
    render_independent_html,
)
from news.storage import Storage  # noqa: E402


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "port.db")
    base = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        art = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url=f"https://eco.sapo.pt/2026/08/08/art-{i}/",
            title=f"Artigo número {i}",
            authors=["Lusa"] if i % 2 == 0 else [],
            published_at=base + timedelta(hours=-i),
            body_text=f"Corpo do artigo {i}.\\nSegundo parágrafo com ação, coração, informação.",
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


class TestIndependentHtml:
    def test_self_contained_no_external_deps(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        # CSS/JS 内嵌
        assert "<style>" in body
        assert "<script>" in body
        # 无外部 CDN / localhost / 相对 CSS/JS 文件引用
        assert "http://" not in body.replace("https://eco.sapo.pt", "")
        assert "https://" not in body.replace("https://eco.sapo.pt", "")
        assert "<link" not in body
        assert "src=" not in body
        # 不依赖 laxinwen / Python / server
        assert "localhost" not in body
        assert "127.0.0.1" not in body

    def test_daily_reader_features_preserved(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        # Daily Reader 标题 / 日期 / 数量
        assert "便携阅读器" in body
        assert "10 articles · ECO" in body
        # TOC
        assert "Table of Contents" in body
        assert 'href="#article-' in body
        # 每篇 section
        assert body.count('class="article"') == 10
        # 已读/收藏/进度/Day-Sepia-Night/J-K/Back to Contents
        assert "read-toggle" in body
        assert "star-toggle" in body
        assert "progress-bar" in body
        assert "sepia" in body
        assert "night" in body
        assert "keydown" in body
        assert "Back to Contents" in body

    def test_ai_status_and_summary(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        assert "✓ AI 已分析" in body
        assert "⚠ AI 分析失败" in body
        assert "○ 尚未分析" in body
        assert "这是一篇中文摘要" in body

    def test_ai_details_embedded(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        # 内嵌 AI 研究详情（关键观点/主题/实体/市场相关性/语言）
        assert "关键观点" in body
        assert "葡萄牙经济有变化" in body
        assert "主题" in body
        assert "Portugal" in body
        assert "MEDIUM" in body

    def test_original_body_and_link(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        assert "Corpo do artigo 0" in body
        assert "ação" in body
        assert "阅读原文" in body
        assert "https://eco.sapo.pt/2026/08/08/art-0/" in body

    def test_beijing_time(self, storage, tmp_path):
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=100)
        body = out.read_text(encoding="utf-8")
        # 最新一篇 UTC 12:00 -> 北京 20:00；且标注北京时间
        assert "2026-08-08 20:00" in body
        assert "北京时间" in body

    def test_html_escape(self, storage, tmp_path):
        art = Article(
            source_id="eco",
            source_name="ECO",
            canonical_url="https://eco.sapo.pt/2026/08/08/escape/",
            title='<script>alert("xss")</script>',
            body_text="<b>bold</b>",
            published_at=datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc),
            status="fetched",
        )
        storage.insert_article(art)
        out = tmp_path / "HKEJ-2026-08-10.html"
        export_independent_html(storage, out, source_id="eco", limit=200)
        body = out.read_text(encoding="utf-8")
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body


class TestPortablePackage:
    def test_structure(self, storage, tmp_path):
        pkg = tmp_path / "HKEJ-2026-08-10"
        export_portable_package(storage, pkg, source_id="eco", limit=100)
        assert (pkg / "index.html").exists()
        articles = sorted(f.name for f in (pkg / "articles").glob("*.html"))
        assert len(articles) == 10
        assert articles[0] == "001.html"
        assert articles[-1] == "010.html"

    def test_index_self_contained(self, storage, tmp_path):
        pkg = tmp_path / "HKEJ-2026-08-10"
        export_portable_package(storage, pkg, source_id="eco", limit=100)
        body = (pkg / "index.html").read_text(encoding="utf-8")
        assert "<style>" in body
        assert "<script>" in body
        assert "http://" not in body.replace("https://eco.sapo.pt", "")
        # index 内提供单篇跳转链接
        assert "查看单篇页" in body
        assert "articles/001.html" in body

    def test_single_pages_render(self, storage, tmp_path):
        pkg = tmp_path / "HKEJ-2026-08-10"
        export_portable_package(storage, pkg, source_id="eco", limit=100)
        single = (pkg / "articles" / "001.html").read_text(encoding="utf-8")
        # 单篇页自包含（内嵌 CSS/JS），可独立双击阅读
        assert "<style>" in single
        assert "<script>" in single
        assert "Corpo do artigo 0" in single
        # 已分析单篇含 AI 详情
        assert "中文摘要" in single
        assert "返回新闻列表" in single

    def test_beijing_time_in_package(self, storage, tmp_path):
        pkg = tmp_path / "HKEJ-2026-08-10"
        export_portable_package(storage, pkg, source_id="eco", limit=100)
        body = (pkg / "index.html").read_text(encoding="utf-8")
        assert "2026-08-08 20:00" in body
        assert "北京时间" in body


class TestRenderIndependent:
    def test_renders_all(self):
        rows = []
        base = datetime(2026, 8, 8, tzinfo=timezone.utc)
        for i in range(3):
            rows.append(
                {
                    "id": i + 1,
                    "title": f"News {i}",
                    "source_name": "ECO",
                    "source_id": "eco",
                    "canonical_url": f"https://eco.sapo.pt/x/{i}/",
                    "published_at": base.isoformat(),
                    "discovered_at": base.isoformat(),
                    "authors": '["Lusa"]' if i % 2 == 0 else "[]",
                    "body_text": f"Body {i}",
                    "ai_status": "ok" if i == 0 else ("failed" if i == 1 else None),
                    "ai_has_failed": 1 if i == 1 else 0,
                }
            )
        html_doc = render_independent_html(
            rows,
            source_name="ECO",
            total=3,
            analyzed_ok=1,
            analyzed_failed=1,
            unanalyzed=1,
            research_rel_by_article={1: "data/export/html/2026/08/0001-x.html"},
            generated_at=datetime(2026, 8, 9, 20, 47, 43, tzinfo=timezone.utc),
            analysis_by_article={1: {"summary_zh": "中文摘要", "key_points": ["k1"]}},
            article_pages={1: "articles/001.html"},
        )
        assert html_doc.count('class="article"') == 3
        assert "Table of Contents" in html_doc
        assert "中文摘要" in html_doc
        assert "查看单篇页" in html_doc
        assert "articles/001.html" in html_doc


class TestPortableReaderPackage:
    """【📦 导出便携阅读包】测试。"""

    def test_structure(self, storage, tmp_path):
        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)
        # 核心结构：index.html + articles/ + server.py + Open-Reader.bat
        assert (pkg / "index.html").exists()
        assert (pkg / "Open-Reader.bat").exists()
        assert (pkg / "server.py").exists()
        articles = sorted(f.name for f in (pkg / "articles").glob("*.html"))
        assert len(articles) == 10
        assert articles[0] == "001.html"

    def test_index_self_contained(self, storage, tmp_path):
        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)
        body = (pkg / "index.html").read_text(encoding="utf-8")
        assert "<style>" in body and "<script>" in body
        assert "查看单篇页" in body
        assert "articles/001.html" in body
        # 不依赖 laxinwen / localhost / 外部 CDN
        assert "localhost" not in body
        assert "127.0.0.1" not in body

    def test_bat_exists_and_no_api_key(self, storage, tmp_path):
        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)
        bat = (pkg / "Open-Reader.bat").read_text(encoding="utf-8")
        # 不含任何 API Key 形态（sk- / 具体 token / secret / key 赋值）
        assert "sk-" not in bat.lower()
        assert "secret" not in bat.lower()
        assert "token" not in bat.lower()
        # 不含绝对路径 / 不绑定某台电脑
        assert "D:" not in bat.upper()
        assert "AIProjects" not in bat
        assert "C:" not in bat.upper()
        # 使用相对定位 %~dp0
        assert "%~dp0" in bat
        # 检测 Python 并给出提示
        assert "python" in bat.lower()
        assert "找不到" in bat or "[错误]" in bat

    def test_bat_no_absolute_path(self, storage, tmp_path):
        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)
        bat = (pkg / "Open-Reader.bat").read_text(encoding="utf-8")
        import re
        assert not re.search(r"[A-Za-z]:\\", bat)

    def test_server_listens_127_only_and_auto_port(self, storage, tmp_path):
        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)
        srv = (pkg / "server.py").read_text(encoding="utf-8")
        # 只监听 127.0.0.1
        assert 'HOST = "127.0.0.1"' in srv
        assert "0.0.0.0" not in srv
        # 自动选择空闲端口
        assert "range(8000, 8010)" in srv
        assert "serve_forever" in srv
        assert "server_close" in srv

    def test_http_access_and_port_release(self, storage, tmp_path):
        """index.html 可通过 HTTP 访问；关闭后端口释放。"""
        import socket
        import subprocess
        import sys
        import time
        import urllib.request

        pkg = tmp_path / "Laxinwen-ECO-2026-08-10"
        export_portable_reader_package(storage, pkg, source_id="eco", limit=100)

        # 找一个空闲端口
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()

        proc = subprocess.Popen(
            [sys.executable, str(pkg / "server.py"), str(pkg), str(free_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1.5)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{free_port}/index.html", timeout=5
            ) as resp:
                body = resp.read().decode("utf-8")
            assert "便携阅读器" in body
            # 文章链接可访问
            with urllib.request.urlopen(
                f"http://127.0.0.1:{free_port}/articles/001.html", timeout=5
            ) as resp:
                assert "Corpo do artigo 0" in resp.read().decode("utf-8")
        finally:
            proc.send_signal(signal.SIGINT)  # 模拟用户关闭窗口 (Ctrl+C)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        time.sleep(0.5)
        # 关闭后端口应释放（进程已退出；用 SO_REUSEADDR 探测可再次绑定）
        released = False
        for _ in range(20):
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", free_port))
                sock.close()
                released = True
                break
            except OSError:
                sock.close()
                time.sleep(0.15)
        if not released:
            pytest.fail("关闭 server 后端口仍被占用")

    def test_default_reader_path(self, storage, tmp_path):
        from news.portable import default_reader_path
        p = default_reader_path("eco")
        assert "Laxinwen" in p.name
        assert "ECO" in p.name
