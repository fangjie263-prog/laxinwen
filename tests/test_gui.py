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
    _DEFAULT_LIMIT,
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
):
    """构造带全部假实现的 app，返回 (app, ctx)。"""
    db = db or (tmp_path / "gui.db")
    pipeline_calls: list[FakePipeline] = []
    processor_calls: list[FakeProcessor] = []
    opened_urls: list[str] = []
    archive_dir = tmp_path / "export" / "news-html"
    research_dir = tmp_path / "export" / "html"

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

    def open_url(url: str):
        opened_urls.append(url)

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
        open_url=open_url,
        news_archive_dir=archive_dir,
        research_dir=research_dir,
    )
    ctx = {
        "db": db,
        "pipeline_calls": pipeline_calls,
        "processor_calls": processor_calls,
        "opened_urls": opened_urls,
        "archive_dir": archive_dir,
        "research_dir": research_dir,
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
        assert root.title() == "ECO News Reader"

    def test_quick_limits_buttons(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        buttons = _find_buttons(app.root, {str(n) for n in _QUICK_LIMITS})
        assert {b.cget("text") for b in buttons} == {"50", "100", "200"}
        for btn in buttons:
            btn.invoke()
            assert app.limit_var.get() == btn.cget("text")

    def test_site_combobox_only_eco(self, root, tmp_path):
        app, _ = _make_app(root, tmp_path)
        assert app.site_var.get() == "eco"
        assert app.site_combo["values"] == ("eco",)

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
    def test_opens_correct_html(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app.limit_var.set("100")
        app._on_open_news_archive()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["opened_urls"]) == 1
        url = ctx["opened_urls"][0]
        assert url.startswith("file://")
        assert "news-html/eco/index.html" in url
        index = ctx["archive_dir"] / "eco" / "index.html"
        assert index.exists()
        assert index.read_text(encoding="utf-8") == "archive eco 100"


class TestAIResearchButton:
    def test_opens_correct_html(self, root, tmp_path):
        app, ctx = _make_app(root, tmp_path)
        app._on_open_research()
        assert _pump_until(app, lambda: not app._busy)

        assert len(ctx["opened_urls"]) == 1
        url = ctx["opened_urls"][0]
        assert url.startswith("file://")
        assert "export/html/index.html" in url
        index = ctx["research_dir"] / "index.html"
        assert index.exists()
        assert index.read_text(encoding="utf-8") == "research eco"


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
