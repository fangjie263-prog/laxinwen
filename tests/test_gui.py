"""GUI 测试 —— ECO News Reader（tkinter，无头环境用 xvfb-run 运行）。

覆盖验收清单：
- 默认抓取数量为 100
- 可以输入 50 / 100 / 200（快捷按钮）
- 非法数量不能执行
- GUI 调用正确的 pipeline（limit / source / db 路径）
- 新闻库按钮打开正确 HTML
- AI research 按钮打开正确 HTML
- AI 分析调用现有 processor（不重新实现 AI provider）
- pipeline 出错后 GUI 不崩溃、按钮恢复
- status 正确读取数据库统计

所有测试均注入假 pipeline / processor / export / open_url，不访问网络。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from news.gui import (  # noqa: E402
    _DEFAULT_AI_LIMIT,
    _DEFAULT_EXPORT_MODE,
    _DEFAULT_LIMIT,
    _EXPORT_OPTIONS,
    _QUICK_LIMITS,
    _NewsReaderApp,
)
from news.model import Article, utcnow  # noqa: E402
from news.storage import Storage  # noqa: E402


# ------------------------------------------------------------------ 工具


def _pump_until(app, predicate, timeout: float = 8.0) -> bool:
    """驱动 tkinter 事件循环直到 predicate 为真（用于等待后台线程完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return True
        time.sleep(0.02)
    app.root.update()
    return predicate()


def _find_buttons(widget, texts: set[str]) -> list[ttk.Button]:
    found: list[ttk.Button] = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button) and child.cget("text") in texts:
            found.append(child)
        found.extend(_find_buttons(child, texts))
    return found


# ------------------------------------------------------------------ 假实现


class FakeStats:
    def __init__(self, **kw):
        self.discovered = kw.get("discovered", 0)
        self.skipped_dup = kw.get("skipped_dup", 0)
        self.fetched_ok = kw.get("fetched_ok", 0)
        self.failed = kw.get("failed", 0)
        self.errors = kw.get("errors", [])


class FakePipeline:
    def __init__(self, storage, limit, *, stats=None, exc=None):
        self.storage = storage
        self.limit = limit
        self.stats = stats or FakeStats(discovered=100, skipped_dup=90, fetched_ok=10)
        self.exc = exc
        self.calls: list = []
        self.closed = False

    def run_site(self, site: str):
        self.calls.append(("run_site", site))
        if self.exc:
            raise self.exc
        return self.stats

    def close(self):
        self.closed = True


class FakeProcessor:
    def __init__(self, storage, *, stats=None, exc=None):
        self.storage = storage
        self.stats = stats or SimpleNamespace(total=3, ok=2, failed=1, errors=[])
        self.exc = exc
        self.calls: list = []
        self.closed = False

    def process_batch(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.stats

    def close(self):
        self.closed = True


def _make_app(
    root,
    tmp_path: Path,
    *,
    db: Path | None = None,
    pipeline_stats=None,
    pipeline_exc=None,
    processor_stats=None,
    processor_exc=None,
    use_real_server: bool = False,
    ai_configured: bool = True,
    ai_config: dict | None = None,
    settings_opened=None,
):
    """构造带全部假实现的 app，返回 (app, ctx)。

    ``use_real_server=False`` 时使用假 server（不真正监听端口）；
    ``use_real_server=True`` 时使用真实 ReaderServer（验证 http://127.0.0.1）。
    """
    db = db or (tmp_path / "gui.db")
    pipeline_calls: list[FakePipeline] = []
    processor_calls: list[FakeProcessor] = []
    opened_urls: list[str] = []
    archive_dir = tmp_path / "export" / "news-html"
    research_dir = tmp_path / "export" / "html"
    portable_dir = tmp_path / "export" / "portable"
    export_root = tmp_path / "export"

    def storage_factory(path):
        return Storage(path)

    def pipeline_factory(storage, limit):
        p = FakePipeline(storage, limit, stats=pipeline_stats, exc=pipeline_exc)
        pipeline_calls.append(p)
        return p

    def processor_factory(storage):
        p = FakeProcessor(storage, stats=processor_stats, exc=processor_exc)
        processor_calls.append(p)
        return p

    def archive_export(storage, out_dir, *, source_id, limit):
        out_dir.mkdir(parents=True, exist_ok=True)
        idx = out_dir / "index.html"
        idx.write_text(f"archive {source_id} {limit}", encoding="utf-8")
        return SimpleNamespace(
            index_path=idx,
            exported=limit,
            analyzed_ok=0,
            analyzed_failed=0,
            unanalyzed=limit,
        )

    def research_export(storage, out_dir, *, source_id):
        out_dir.mkdir(parents=True, exist_ok=True)
        idx = out_dir / "index.html"
        idx.write_text(f"research {source_id}", encoding="utf-8")
        return SimpleNamespace(index_path=idx, analysis_ok=0, analysis_failed=0)

    portable_calls: list[dict] = []

    def portable_html_export(storage, out_path, *, source_id, limit, research_root=None):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"independent {source_id} {limit}", encoding="utf-8")
        portable_calls.append(("html", source_id, limit))
        return SimpleNamespace(exported=limit, analyzed_ok=0, analyzed_failed=0, unanalyzed=limit)

    def portable_package_export(storage, out_dir, *, source_id, limit, research_root=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        idx = out_dir / "index.html"
        idx.write_text(f"pkg {source_id} {limit}", encoding="utf-8")
        portable_calls.append(("pkg", source_id, limit))
        return SimpleNamespace(index_path=idx, exported=limit, analyzed_ok=0, analyzed_failed=0, unanalyzed=limit)

    def portable_reader_export(storage, out_dir, *, source_id, limit, research_root=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        idx = out_dir / "index.html"
        idx.write_text(f"reader {source_id} {limit}", encoding="utf-8")
        (out_dir / "Open-Reader.bat").write_text("@echo off", encoding="utf-8")
        portable_calls.append(("reader", source_id, limit))
        return SimpleNamespace(index_path=idx, exported=limit, analyzed_ok=0, analyzed_failed=0, unanalyzed=limit)

    def open_url(url: str):
        opened_urls.append(url)

    class _FakeServer:
        """假 HTTP server：记录 URL 生成，不真正监听端口（离线测试用）。"""

        def __init__(self, root_dir):
            self.root_dir = Path(root_dir)
            self.port = 8765
            self.stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True

        def url_for(self, rel_path: str) -> str:
            return f"http://127.0.0.1:{self.port}/{rel_path.lstrip('/')}"

    def fake_server_factory(export_root):
        return _FakeServer(export_root)

    server_factory = None if use_real_server else fake_server_factory

    # ---- 假 AI 配置中心 ----------------
    ai_config = ai_config or {}
    cfg_state = {
        "provider": ai_config.get("provider", "openai-compatible"),
        "base_url": ai_config.get("base_url", "https://api.example.com/v1" if ai_configured else ""),
        "api_key": ai_config.get("api_key", "sk-fake-key" if ai_configured else ""),
        "model": ai_config.get("model", "test-model" if ai_configured else ""),
    }
    if settings_opened is None:
        settings_opened = []
    test_calls: list[dict] = []

    class _FakeConfigStore:
        @staticmethod
        def read_config():
            from news.ai.config_store import AiConfig
            return AiConfig(**cfg_state)

        @staticmethod
        def save_config(cfg):
            cfg_state.update(
                provider=cfg.provider, base_url=cfg.base_url,
                api_key=cfg.api_key, model=cfg.model,
            )
            return Path(tmp_path) / ".env"

        @staticmethod
        def masked(value):
            from news.ai.config_store import masked as _m
            return _m(value)

    def fake_test_connection(config):
        from news.ai.provider import TestConnectionResult
        test_calls.append(config)
        return TestConnectionResult(ok=True, message="✅ 测试成功", kind="ok",
                                    provider=config.provider, model=config.model)

    def fake_show_settings(parent, *, current=None, on_save=None):
        settings_opened.append(current)
        # 调用 on_save 模拟用户在对话框里点击“保存”
        if on_save is not None:
            from news.ai.config_store import AiConfig
            on_save(AiConfig(
                provider=cfg_state["provider"],
                base_url=cfg_state["base_url"],
                api_key=cfg_state["api_key"],
                model=cfg_state["model"],
            ))

    app = _NewsReaderApp(
        root,
        db_path=db,
        site="eco",
        site_name="ECO",
        storage_factory=storage_factory,
        pipeline_factory=pipeline_factory,
        processor_factory=processor_factory,
        news_archive_export=archive_export,
        research_export=research_export,
        portable_html_export=portable_html_export,
        portable_package_export=portable_package_export,
        portable_reader_export=portable_reader_export,
        open_url=open_url,
        news_archive_dir=archive_dir,
        research_dir=research_dir,
        portable_dir=portable_dir,
        export_root=export_root,
        server_factory=server_factory,
        ai_config_store=_FakeConfigStore,
        ai_test_connection=fake_test_connection,
        ai_show_settings=fake_show_settings,
    )
    ctx = {
        "db": db,
        "pipeline_calls": pipeline_calls,
        "processor_calls": processor_calls,
        "opened_urls": opened_urls,
        "archive_dir": archive_dir,
        "research_dir": research_dir,
        "portable_dir": portable_dir,
        "export_root": export_root,
        "portable_calls": portable_calls,
        "settings_opened": settings_opened,
        "app": app,
    }
    return app, ctx


def _log_text(app) -> str:
    return app.log_text.get("1.0", "end")


# ------------------------------------------------------------------ fixture


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:  # 无显示环境（应通过 xvfb-run 运行）
        pytest.skip(f"无图形显示环境: {exc}")
    r.update()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


# ------------------------------------------------------------------ 测试


class TestBasics:
    def test_default_limit_is_100(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        assert app.limit_var.get() == str(_DEFAULT_LIMIT) == "100"
        assert app.ai_limit_var.get() == str(_DEFAULT_AI_LIMIT) == "3"

    def test_window_title(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        assert root.title() == "Laxinwen News Reader"

    def test_quick_limits_buttons(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        buttons = _find_buttons(app.root, {str(n) for n in _QUICK_LIMITS})
        assert {b.cget("text") for b in buttons} == {"50", "100", "200"}
        for btn in buttons:
            btn.invoke()
            assert app.limit_var.get() == btn.cget("text")

    def test_site_combobox_has_all_sources(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        assert app.site_var.get() == "eco"
        assert app.site_combo["values"] == ("eco", "hkej", "all")

    def test_source_switch_reflects_selection(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        # 切换到 HKEJ
        app.site_combo.set("hkej")
        app._on_source_changed()
        assert app._selected_site_ids() == ("hkej",)
        assert "当前来源：HKEJ" in app.status_labels["current_source"].cget("text")
        # 切换到全部
        app.site_combo.set("all")
        app._on_source_changed()
        assert app._selected_site_ids() == ("eco", "hkej")
        assert "当前来源：全部" in app.status_labels["current_source"].cget("text")

    def test_invalid_limit_cannot_run(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        for bad in ("abc", "0", "-5", "1.5", ""):
            app.limit_var.set(bad)
            app._on_fetch()
            assert not ctx["pipeline_calls"], f"非法数量 {bad!r} 不应触发抓取"
        assert "无效的抓取数量" in _log_text(app)

    def test_invalid_ai_limit_cannot_run(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.ai_limit_var.set("xyz")
        app._on_ai_analyze()
        assert not ctx["processor_calls"]
        assert "无效的AI 分析数量" in _log_text(app)


class TestPipelineCall:
    def test_gui_calls_correct_pipeline(self, root, tmp_path):
        app, ctx = _make_app(
            root, tmp_path, pipeline_stats=FakeStats(
                discovered=100, skipped_dup=90, fetched_ok=10, failed=0
            )
        )
        app.limit_var.set("100")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["pipeline_calls"]) == 1
        pipe = ctx["pipeline_calls"][0]
        assert pipe.limit == 100
        assert pipe.storage.db_path == ctx["db"]
        assert pipe.calls == [("run_site", "eco")]
        assert pipe.closed is True

        log = _log_text(app)
        assert "开始抓取" in log
        assert "发现：100" in log
        assert "重复：90" in log
        assert "新增：10" in log
        assert "失败：0" in log
        assert "抓取完成" in log

    def test_fetch_uses_existing_dedup_no_duplicate(self, root, tmp_path):
        """第二次抓取时 pipeline 返回 重复=100 / 新增=0（去重逻辑在 pipeline 内）。"""
        app, ctx = _make_app(
            root, tmp_path, pipeline_stats=FakeStats(
                discovered=100, skipped_dup=100, fetched_ok=0, failed=0
            )
        )
        app.limit_var.set("100")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["pipeline_calls"]) == 2
        log = _log_text(app)
        assert log.count("发现：100") >= 2
        assert "重复：100" in log
        assert "新增：0" in log

    def test_pipeline_error_does_not_crash(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, pipeline_exc=RuntimeError("HTTP 500"))
        app.limit_var.set("100")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)

        log = _log_text(app)
        assert "抓取失败" in log
        assert "HTTP 500" in log
        # 按钮恢复，可再次点击
        assert str(app.fetch_btn.cget("state")) == "normal"
        app.limit_var.set("50")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        assert len(ctx["pipeline_calls"]) == 2


class TestNewsArchiveButton:
    def test_opens_http_localhost_not_file(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.limit_var.set("100")
        app._on_open_news_archive()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["opened_urls"]) == 1
        url = ctx["opened_urls"][0]
        # 必须打开 http://127.0.0.1，而不是 file://
        assert url.startswith("http://127.0.0.1:")
        assert "file://" not in url
        assert "news-html/eco/index.html" in url
        index = ctx["archive_dir"] / "eco" / "index.html"
        assert index.exists()
        assert index.read_text(encoding="utf-8") == "archive eco 100"
        # 日志中明确显示本地 HTTP 地址
        log = _log_text(app)
        assert "新闻库已启动" in log
        assert url in log

    def test_opens_via_real_http_server(self, root, tmp_path):
        """真实 ReaderServer：浏览器打开 http://127.0.0.1 且文件可通过 HTTP 读取。"""
        app, ctx = _make_app(root, tmp_path, use_real_server=True)
        app.limit_var.set("100")
        app._on_open_news_archive()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["opened_urls"]) == 1
        url = ctx["opened_urls"][0]
        assert url.startswith("http://127.0.0.1:")
        assert "file://" not in url
        assert "news-html/eco/index.html" in url
        # 通过 HTTP 实际可读
        import urllib.request

        body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        assert "archive eco 100" in body
        # 关闭 GUI（走 WM_DELETE_WINDOW 协议）后服务器停止
        app._on_close()
        assert app._http_server is None or not app._http_server.running


class TestAIResearchButton:
    def test_opens_http_localhost_not_file(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app._on_open_research()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["opened_urls"]) == 1
        url = ctx["opened_urls"][0]
        # 必须打开 http://127.0.0.1，而不是 file://
        assert url.startswith("http://127.0.0.1:")
        assert "file://" not in url
        assert "html/eco/index.html" in url
        index = ctx["research_dir"] / "eco" / "index.html"
        assert index.exists()
        assert index.read_text(encoding="utf-8") == "research eco"
        log = _log_text(app)
        assert "AI 研究结果已启动" in log
        assert url in log


class TestAIProcess:
    def test_calls_existing_processor(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.ai_limit_var.set("3")
        app._on_ai_analyze()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["processor_calls"]) == 1
        proc = ctx["processor_calls"][0]
        assert proc.calls == [{"source_id": "eco", "limit": 3}]
        assert proc.closed is True
        log = _log_text(app)
        assert "AI 处理" in log
        assert "成功 2" in log
        assert "失败 1" in log

    def test_processor_error_does_not_crash(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, processor_exc=RuntimeError("HTTP 401"))
        app.ai_limit_var.set("3")
        app._on_ai_analyze()
        assert _pump_until(app, lambda: not app._busy)

        log = _log_text(app)
        assert "AI 分析失败" in log
        assert "HTTP 401" in log
        assert str(app.ai_btn.cget("state")) == "normal"


class TestStatus:
    def test_status_reads_db_statistics(self, root, tmp_path):
        db = tmp_path / "seeded.db"
        s = Storage(db)
        try:
            for i in range(7):
                art = Article(
                    source_id="eco",
                    source_name="ECO",
                    canonical_url=f"https://eco.sapo.pt/2026/08/08/art-{i}/",
                    title=f"Artigo {i}",
                    published_at=utcnow(),
                    body_text="corpo",
                    language="pt-PT",
                    status="fetched",
                )
                aid, _ = s.insert_article(art)
                if i < 2:
                    s.upsert_analysis(
                        article_id=aid, provider="p", model="m", prompt_version="v1",
                        summary_zh="s", key_points=[], topics=[], entities=[],
                        market_relevance="low", market_relevance_reason="", language="",
                        status="success",
                    )
                elif i == 2:
                    s.upsert_analysis(
                        article_id=aid, provider="p", model="m", prompt_version="v1",
                        summary_zh="", key_points=[], topics=[], entities=[],
                        market_relevance="low", market_relevance_reason="", language="",
                        status="failed",
                    )
        finally:
            s.close()

        app, ctx = _make_app(root, tmp_path, db=db)
        texts = {k: v.cget("text") for k, v in app.status_labels.items()}
        assert "ECO 新闻：7" in texts["eco_count"]
        assert "AI 已分析：2" in texts["ai_ok"]
        assert "AI 失败：1" in texts["ai_failed"]
        assert texts["db"].startswith("数据库：")
        assert str(ctx["db"]) in texts["db"]
        assert "最后抓取" in texts["last_fetch"]

    def test_status_refreshes_after_fetch(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        before = app.status_labels["eco_count"].cget("text")
        assert "ECO 新闻：0" in before
        app.limit_var.set("100")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        # 抓取完成后重新读取状态（fake pipeline 不写库，仍应为 0，但不崩溃）
        after = app.status_labels["eco_count"].cget("text")
        assert "ECO 新闻：" in after

    def test_status_shows_both_eco_and_hkej_counts(self, root, tmp_path):
        db = tmp_path / "both.db"
        s = Storage(db)
        try:
            for i in range(7):
                art = Article(
                    source_id="eco", source_name="ECO",
                    canonical_url=f"https://eco.sapo.pt/2026/08/08/a-{i}/",
                    title=f"E {i}", published_at=utcnow(), body_text="x", language="pt-PT",
                    status="fetched",
                )
                s.insert_article(art)
            for i in range(3):
                art = Article(
                    source_id="hkej", source_name="HKEJ",
                    canonical_url=f"https://www1.hkej.com/dailynews/finance/article/{i}",
                    title=f"H {i}", published_at=utcnow(), body_text="x", language="zh-Hant",
                    status="fetched",
                )
                s.insert_article(art)
        finally:
            s.close()

        app, ctx = _make_app(root, tmp_path, db=db)
        texts = {k: v.cget("text") for k, v in app.status_labels.items()}
        assert "ECO 新闻：7" in texts["eco_count"]
        assert "HKEJ 新闻：3" in texts["hkej_count"]
        assert "当前来源：ECO" in texts["current_source"]
        assert "最后操作：" in texts["last_action"]


class TestMultiSourceFetch:
    """Phase 2：来源选择后抓取必须调用正确的 site。"""

    def _run_fetch_for_source(self, app, ctx, source: str, limit: int):
        app.site_combo.set(source)
        app._on_source_changed()
        app.limit_var.set(str(limit))
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        return [p.calls for p in ctx["pipeline_calls"]]

    def test_eco_50_calls_site_eco(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_fetch_for_source(app, ctx, "eco", 50)
        assert calls == [[("run_site", "eco")]]
        pipe = ctx["pipeline_calls"][0]
        assert pipe.limit == 50

    def test_hkej_50_calls_site_hkej(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_fetch_for_source(app, ctx, "hkej", 50)
        assert calls == [[("run_site", "hkej")]]
        pipe = ctx["pipeline_calls"][0]
        assert pipe.limit == 50

    def test_all_50_calls_both_sites(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_fetch_for_source(app, ctx, "all", 50)
        assert calls == [[("run_site", "eco"), ("run_site", "hkej")]]
        pipe = ctx["pipeline_calls"][0]
        assert pipe.limit == 50
        log = _log_text(app)
        assert "[ECO] 发现" in log
        assert "[HKEJ] 发现" in log

    def test_all_50_logs_both_sources(self, root, tmp_path):
        app, ctx = _make_app(
            root, tmp_path, pipeline_stats=FakeStats(discovered=50, fetched_ok=50)
        )
        calls = self._run_fetch_for_source(app, ctx, "all", 50)
        log = _log_text(app)
        assert "[ECO]" in log and "[HKEJ]" in log
        assert "发现：50" in log

    def test_custom_limit_input(self, root, tmp_path):
        """允许任意正整数（如 20 / 500）并做基本校验。"""
        app, ctx = _make_app(root, tmp_path)
        for valid in ("20", "500"):
            app.limit_var.set(valid)
            app._on_fetch()
            assert _pump_until(app, lambda: not app._busy)
        assert len(ctx["pipeline_calls"]) == 2
        assert ctx["pipeline_calls"][0].limit == 20
        assert ctx["pipeline_calls"][1].limit == 500

    def test_invalid_custom_limit_rejected(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        for bad in ("0", "-1", "abc"):
            app.limit_var.set(bad)
            app._on_fetch()
            assert not ctx["pipeline_calls"], f"非法数量 {bad!r} 不应触发抓取"


class TestMultiSourceNewsArchive:
    """Phase 2：打开新闻库必须按来源打开正确页面。"""

    def _open_archive(self, app, ctx, source: str, limit: int = 50):
        app.site_combo.set(source)
        app._on_source_changed()
        app.limit_var.set(str(limit))
        app._on_open_news_archive()
        assert _pump_until(app, lambda: not app._busy)
        return ctx["opened_urls"]

    def test_eco_opens_eco_archive(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        urls = self._open_archive(app, ctx, "eco")
        assert len(urls) == 1
        assert "news-html/eco/index.html" in urls[0]
        assert "file://" not in urls[0]
        assert (ctx["archive_dir"] / "eco" / "index.html").exists()

    def test_hkej_opens_hkej_archive(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        urls = self._open_archive(app, ctx, "hkej")
        assert len(urls) == 1
        assert "news-html/hkej/index.html" in urls[0]
        assert "file://" not in urls[0]
        assert (ctx["archive_dir"] / "hkej" / "index.html").exists()

    def test_all_opens_both_archives(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        urls = self._open_archive(app, ctx, "all")
        assert len(urls) == 2
        assert any("news-html/eco/index.html" in u for u in urls)
        assert any("news-html/hkej/index.html" in u for u in urls)


class TestMultiSourceAI:
    """Phase 2：AI 分析按来源调用现有 processor。"""

    def _run_ai(self, app, ctx, source: str, limit: int = 3):
        app.site_combo.set(source)
        app._on_source_changed()
        app.ai_limit_var.set(str(limit))
        app._on_ai_analyze()
        assert _pump_until(app, lambda: not app._busy)
        return [p.calls for p in ctx["processor_calls"]]

    def test_eco_ai_calls_eco_processor(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_ai(app, ctx, "eco")
        assert calls == [[{"source_id": "eco", "limit": 3}]]

    def test_hkej_ai_calls_hkej_processor(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_ai(app, ctx, "hkej")
        assert calls == [[{"source_id": "hkej", "limit": 3}]]

    def test_all_ai_calls_both_processors(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        calls = self._run_ai(app, ctx, "all")
        assert calls == [[{"source_id": "eco", "limit": 3}, {"source_id": "hkej", "limit": 3}]]


class TestMultiSourceResearch:
    """Phase 2：AI 研究结果按来源打开。"""

    def test_eco_research_opens_eco_dir(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.site_combo.set("eco")
        app._on_source_changed()
        app._on_open_research()
        assert _pump_until(app, lambda: not app._busy)
        urls = ctx["opened_urls"]
        assert len(urls) == 1
        assert "html/eco/index.html" in urls[0]

    def test_hkej_research_opens_hkej_dir(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.site_combo.set("hkej")
        app._on_source_changed()
        app._on_open_research()
        assert _pump_until(app, lambda: not app._busy)
        urls = ctx["opened_urls"]
        assert len(urls) == 1
        assert "html/hkej/index.html" in urls[0]


class TestPortableExportButtons:
    """导出方式下拉 + 单一导出按钮（便携阅读包 / 独立 HTML / HTML 新闻包）。"""

    def _set_mode(self, app, mode: str):
        """通过下拉选择导出方式（mode: reader/html/package）。"""
        label = dict(_EXPORT_OPTIONS)[mode]
        app.export_mode_var.set(label)

    def test_export_dropdown_options_and_default(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        assert app.export_mode_combo["values"] == (
            "📦 便携阅读包",
            "📄 独立 HTML",
            "📚 HTML 新闻包",
        )
        # 默认导出方式 = 便携阅读包
        assert app.export_mode_var.get() == "📦 便携阅读包"
        assert _DEFAULT_EXPORT_MODE == "reader"
        assert app._selected_export_mode() == "reader"

    def test_default_export_calls_portable_reader(self, root, tmp_path):
        """验收 A：默认导出方式=便携阅读包，点击「导出」调用 portable reader。"""
        app, ctx = _make_app(root, tmp_path)
        app.export_limit_var.set("100")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert ctx["portable_calls"] == [("reader", "eco", 100)]
        log = _log_text(app)
        assert "便携阅读包" in log
        assert "Open-Reader.bat" in log
        assert "127.0.0.1" in log

    def test_select_reader_calls_portable_reader(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        self._set_mode(app, "reader")
        app.export_limit_var.set("30")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert ctx["portable_calls"] == [("reader", "eco", 30)]

    def test_select_html_calls_independent_html(self, root, tmp_path):
        """验收 B：选择「独立 HTML」→ 调用 portable html。"""
        app, ctx = _make_app(root, tmp_path)
        self._set_mode(app, "html")
        app.export_limit_var.set("100")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert ctx["portable_calls"] == [("html", "eco", 100)]
        log = _log_text(app)
        assert "独立 HTML" in log
        assert "双击即可阅读" in log

    def test_select_package_calls_package(self, root, tmp_path):
        """验收 B：选择「HTML 新闻包」→ 调用 package。"""
        app, ctx = _make_app(root, tmp_path)
        self._set_mode(app, "package")
        app.export_limit_var.set("50")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert ctx["portable_calls"] == [("pkg", "eco", 50)]
        log = _log_text(app)
        assert "HTML 新闻包" in log
        assert "index.html" in log

    def test_export_all_source_both(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.site_combo.set("all")
        app._on_source_changed()
        self._set_mode(app, "html")
        app.export_limit_var.set("100")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert ctx["portable_calls"] == [("html", "eco", 100), ("html", "hkej", 100)]

    def test_invalid_limit_rejected(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.export_limit_var.set("abc")
        app._on_export()
        assert not ctx["portable_calls"]
        assert "无效的导出数量" in _log_text(app)

    def test_error_does_not_crash(self, root, tmp_path):
        # 覆盖导出抛错：GUI 不崩溃、按钮恢复
        app, _ = _make_app(root, tmp_path)

        def boom(*a, **k):
            raise RuntimeError("write error")

        app._portable_html_export = boom
        app.export_limit_var.set("100")
        self._set_mode(app, "html")
        app._on_export()
        assert _pump_until(app, lambda: not app._busy)
        assert "导出（📄 独立 HTML）失败" in _log_text(app)
        assert str(app.export_btn.cget("state")) == "normal"

    def test_reader_error_does_not_crash(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)

        def boom(*a, **k):
            raise RuntimeError("write error")

        app._portable_reader_export = boom
        app.export_limit_var.set("100")
        app._on_export()  # 默认 reader
        assert _pump_until(app, lambda: not app._busy)
        assert "导出（📦 便携阅读包）失败" in _log_text(app)
        assert str(app.export_btn.cget("state")) == "normal"


class TestMultiSourceErrors:
    """Phase 2：任意来源失败，GUI 都不能崩溃、按钮必须恢复。"""

    def test_eco_http500_does_not_crash(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, pipeline_exc=RuntimeError("HTTP 500"))
        app.site_combo.set("eco")
        app._on_source_changed()
        app.limit_var.set("50")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        assert str(app.fetch_btn.cget("state")) == "normal"
        assert "抓取失败" in _log_text(app)

    def test_hkej_http500_does_not_crash(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, pipeline_exc=RuntimeError("HTTP 500"))
        app.site_combo.set("hkej")
        app._on_source_changed()
        app.limit_var.set("50")
        app._on_fetch()
        assert _pump_until(app, lambda: not app._busy)
        assert str(app.fetch_btn.cget("state")) == "normal"
        assert "抓取失败" in _log_text(app)

    def test_ai_http401_does_not_crash(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, processor_exc=RuntimeError("HTTP 401"))
        app.site_combo.set("hkej")
        app._on_source_changed()
        app._on_ai_analyze()
        assert _pump_until(app, lambda: not app._busy)
        assert str(app.ai_btn.cget("state")) == "normal"
        assert "AI 分析失败" in _log_text(app)


class TestAiSettings:
    """AI 设置入口 + 未配置时的引导。"""

    def test_ai_settings_button_opens_settings(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path, ai_configured=True)
        app._on_ai_settings()
        assert len(ctx["settings_opened"]) == 1
        assert ctx["settings_opened"][0].provider == "openai-compatible"

    def test_ai_settings_save_updates_config(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app._on_ai_settings()
        # 假对话框 on_save 使用当前 cfg_state；这里验证保存后日志含掩码、无明文 Key
        log = _log_text(app)
        assert "AI 配置已保存并立即生效" in log
        assert "sk-fake-key" not in log  # 日志绝不含完整 Key
        assert "API Key=" in log

    def test_ai_status_label_unconfigured(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path, ai_configured=False)
        assert "⚪ 未配置" in app._ai_status_label()
        assert "⚪ 未配置" in app.status_labels["ai_status"].cget("text")

    def test_ai_status_label_configured(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path, ai_configured=True)
        assert "🟢 已配置" in app._ai_status_label()

    def test_ai_analyze_unconfigured_prompts_settings(self, root, tmp_path, monkeypatch):
        """验收 C：无 AI 配置时点 AI 分析 → 提示进入 AI 设置。"""
        app, ctx = _make_app(root, tmp_path, ai_configured=False)
        answers = iter([True])
        monkeypatch.setattr(
            "news.gui.messagebox.askyesno", lambda *a, **k: next(answers)
        )
        app.ai_limit_var.set("3")
        app._on_ai_analyze()
        assert len(ctx["settings_opened"]) == 1  # 已打开 AI 设置
        assert not ctx["processor_calls"]  # 未直接执行 AI 分析

    def test_ai_analyze_unconfigured_cancel_no_processor(self, root, tmp_path, monkeypatch):
        app, ctx = _make_app(root, tmp_path, ai_configured=False)
        monkeypatch.setattr(
            "news.gui.messagebox.askyesno", lambda *a, **k: False
        )
        app.ai_limit_var.set("3")
        app._on_ai_analyze()
        assert not ctx["settings_opened"]
        assert not ctx["processor_calls"]
        assert "尚未配置 AI" in _log_text(app)

    def test_ai_analyze_configured_runs_processor(self, root, tmp_path):
        """情况 A：AI 已配置则正常执行 AI 分析。"""
        app, ctx = _make_app(root, tmp_path, ai_configured=True)
        app.ai_limit_var.set("3")
        app._on_ai_analyze()
        assert _pump_until(app, lambda: not app._busy)
        assert len(ctx["processor_calls"]) == 1
        assert not ctx["settings_opened"]
