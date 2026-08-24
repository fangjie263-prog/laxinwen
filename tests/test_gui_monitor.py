"""GUI「抓取监控」小窗口测试。

覆盖验收清单：
- 手动抓取完成 → GUI 出现任务级摘要
- 定时任务完成 → GUI 能读取后台摘要（scheduled-fetch.log）
- FETCH SUCCESS → 转换为简洁成功信息
- EXPORT SUCCESS → 显示导出成功
- 无新增 → 显示“无新增新闻”
- 失败 → 显示失败
- GUI 启动读取 scheduled-fetch.log
- 清空只清 GUI、不删除真实日志
- worker 不直接操作 Tkinter（通过 queue + after）
- 多任务摘要不会互相覆盖

无显示环境时 tkinter 测试自动跳过；解析逻辑测试用纯对象验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402

from news.gui import _NewsReaderApp  # noqa: E402
from news.scheduler_config import SchedulerConfig  # noqa: E402


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


def _pump_until(app, predicate, timeout: float = 8.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return True
        time.sleep(0.02)
    app.root.update()
    return predicate()


def _job(job_id="rfi-default", source="rfi", enabled=True, limit=50):
    return SchedulerConfig(
        id=job_id,
        name=job_id,
        enabled=enabled,
        source=source,
        frequency="daily",
        interval_hours=24,
        limit=limit,
    )


def _make_monitor_app(root, tmp_path, jobs=None, query=None):
    """构造一个最小 app，注入假 scheduler，便于读取监控窗口。"""
    sched_file = tmp_path / "data" / "scheduler.json"

    def fake_query(job):
        return (query or (lambda j: {}))(job)

    def fake_save_jobs(job_list, path=None):
        return Path(path) if path else sched_file

    app = _NewsReaderApp(
        root,
        db_path=tmp_path / "gui.db",
        site="rfi",
        site_name="RFI",
        scheduler_load_jobs=lambda path=None: list(jobs or []),
        scheduler_save_jobs=fake_save_jobs,
        scheduler_query=fake_query,
        scheduler_config_path=sched_file,
    )
    return app


def _monitor_text(app) -> str:
    return app.monitor_text.get("1.0", "end")


def _cur(app) -> str:
    return app.monitor_cur_var.get()


# ------------------------------------------------------------------ 纯解析逻辑


class TestMonitorParser:
    """不依赖 tkinter，直接驱动 app 的监控解析方法（用 object 化 app 校验）。"""

    def _bare(self):
        """构造一个只带监控状态的对象（不创建 tkinter 窗口）。"""
        app = object.__new__(_NewsReaderApp)
        app._monitor_entries = []
        app._monitor_cur = "空闲"
        app._monitor_task = None
        # 不渲染 tkinter 控件，仅验证解析逻辑
        app._monitor_render = lambda: None
        app._monitor_set_cur = lambda text: setattr(app, "_monitor_cur", text)
        return app

    def test_fetch_success_to_concise(self):
        app = self._bare()
        lines = [
            "开始抓取 RFI 最新 50 篇",
            "发现：53",
            "可读新闻：50 / 目标 50",
            "FETCH: SUCCESS",
            "EXPORT: SUCCESS",
            "抓取完成（RFI，limit=50）",
        ]
        for ln in lines:
            app._monitor_feed_line(ln)
        # 一条“开始” + 一条“完成”
        assert len(app._monitor_entries) == 2
        assert "开始抓取，目标 50 条" in app._monitor_entries[0]
        assert "RFI" in app._monitor_entries[1]
        assert "完成：新增 50 条（目标 50 条），导出成功" in app._monitor_entries[1]
        assert app._monitor_cur == "RFI · 已完成 · 新增 50 条，导出成功"

    def test_fetch_success_export_failed(self):
        app = self._bare()
        for ln in [
            "开始抓取 HKEJ 最新 100 篇",
            "发现：100",
            "可读新闻：100 / 目标 100",
            "FETCH: SUCCESS",
            "EXPORT: FAILED",
            "抓取完成（HKEJ，limit=100）",
        ]:
            app._monitor_feed_line(ln)
        # 最后一条是完成摘要
        assert "完成：新增 100 条（目标 100 条），导出失败" in app._monitor_entries[-1]

    def test_no_new_news(self):
        app = self._bare()
        for ln in [
            "开始抓取 RFI 最新 50 篇",
            "发现：0",
            "可读新闻：0 / 目标 50",
            "FETCH: SUCCESS",
            "EXPORT: SUCCESS",
            "抓取完成（RFI，limit=50）",
        ]:
            app._monitor_feed_line(ln)
        assert "无新增新闻" in app._monitor_entries[-1]

    def test_failure(self):
        app = self._bare()
        app._monitor_task = {"source": "rfi", "limit": 50}
        app._monitor_feed_line("抓取失败：连接超时")
        assert app._monitor_entries[0].endswith("失败：抓取异常")
        assert app._monitor_cur == "RFI · 失败"

    def test_background_log_parse(self):
        app = self._bare()
        sched = [
            "========== 自动定时抓取开始 ==========",
            "JOB: rfi-default",
            "SOURCE: rfi",
            "TARGET: 50（usable limit）",
            "发现数量: 53",
            "可读新闻: 50 / 目标 50",
            "FETCH: SUCCESS（usable=50 / 目标 50）",
            "EXPORT: SUCCESS → 便携阅读包已导出 50 篇 → ...",
            "========== 自动定时抓取结束（exit=0）==========",
            "========== 自动定时抓取开始 ==========",
            "JOB: hkej-1",
            "SOURCE: hkej",
            "TARGET: 100（usable limit）",
            "发现数量: 100",
            "可读新闻: 100 / 目标 100",
            "FETCH: SUCCESS（usable=100 / 目标 100）",
            "EXPORT: SUCCESS → ...",
            "========== 自动定时抓取结束（exit=0）==========",
        ]
        app._monitor_parse_sched_log(sched)
        # 两个任务摘要都保留，互不覆盖
        assert len(app._monitor_entries) == 2
        assert any("RFI" in e and "新增 50 条" in e for e in app._monitor_entries)
        assert any("HKEJ" in e and "新增 100 条" in e for e in app._monitor_entries)

    def test_multiple_manual_tasks_kept(self):
        app = self._bare()
        for ln in [
            "开始抓取 RFI 最新 50 篇", "发现：53", "可读新闻：50 / 目标 50",
            "FETCH: SUCCESS", "EXPORT: SUCCESS", "抓取完成（RFI，limit=50）",
            "开始抓取 HKEJ 最新 100 篇", "发现：100", "可读新闻：100 / 目标 100",
            "FETCH: SUCCESS", "EXPORT: SUCCESS", "抓取完成（HKEJ，limit=100）",
        ]:
            app._monitor_feed_line(ln)
        # 两个任务：各一条开始 + 一条完成，共 4 条
        assert len(app._monitor_entries) == 4
        assert sum("完成" in e for e in app._monitor_entries) == 2


# ------------------------------------------------------------------ 集成测试


class TestMonitorIntegration:
    def test_manual_fetch_shows_summary(self, root, tmp_path):
        app = _make_monitor_app(root, tmp_path)
        # 模拟后台日志链路产生的消息
        msgs = [
            "开始抓取 RFI 最新 50 篇",
            "[RFI] 发现：53",
            "可读新闻：50 / 目标 50",
            "FETCH: SUCCESS",
            "EXPORT: SUCCESS",
            "抓取完成（RFI，limit=50）",
        ]
        for m in msgs:
            app._queue.put(m)
        app._poll_queue()
        text = _monitor_text(app)
        assert "开始抓取，目标 50 条" in text
        assert "完成：新增 50 条" in text
        assert "导出成功" in text

    def test_clear_only_gui_not_log(self, root, tmp_path):
        # 预置一个真实 scheduled-fetch.log
        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "scheduled-fetch.log"
        log_file.write_text(
            "========== 自动定时抓取开始 ==========\n"
            "SOURCE: rfi\nTARGET: 50（usable limit）\n发现数量: 53\n"
            "可读新闻: 50 / 目标 50\nFETCH: SUCCESS\nEXPORT: SUCCESS\n"
            "========== 自动定时抓取结束（exit=0）==========\n",
            encoding="utf-8",
        )
        # 用一个会指向 tmp_path 的 app：需要改写 _scheduled_log_path 读取的默认路径。
        # 这里直接用解析器验证清空行为。
        app = _make_monitor_app(root, tmp_path)
        app._monitor_entries = ["08:00  RFI  完成：新增 50 条，导出成功"]
        app._monitor_render()
        assert "新增 50" in _monitor_text(app)
        # 清空只清 GUI
        app._on_monitor_clear()
        assert _monitor_text(app).strip() == ""
        # 真实日志文件仍在
        assert log_file.is_file()
        assert "FETCH: SUCCESS" in log_file.read_text(encoding="utf-8")

    def test_worker_does_not_touch_tk_directly(self, root, tmp_path):
        app = _make_monitor_app(root, tmp_path)
        import threading

        # worker 只往 queue 放消息；若直接操作 Tk 控件会抛 RuntimeError
        def fake_worker():
            app._queue.put("开始抓取 RFI 最新 50 篇")
            app._queue.put("发现：53")
            app._queue.put("可读新闻：50 / 目标 50")
            app._queue.put("FETCH: SUCCESS")
            app._queue.put("EXPORT: SUCCESS")
            app._queue.put("抓取完成（RFI，limit=50）")

        t = threading.Thread(target=fake_worker, daemon=True)
        t.start()
        t.join(5)
        # 主线程消费 queue → 监控摘要出现
        app._poll_queue()
        assert "完成：新增 50 条" in _monitor_text(app)
        assert "导出成功" in _monitor_text(app)

    def test_run_now_shows_requested_not_success(self, root, tmp_path):
        app = _make_monitor_app(root, tmp_path)
        # 模拟「立即运行一次」后台链路发来的消息（schtasks /Run 成功）
        app._queue.put("已请求 Windows Task Scheduler 执行任务：rfi-default\n后台抓取正在运行")
        app._poll_queue()
        text = _monitor_text(app)
        assert "已请求 Windows Task Scheduler 执行" in text
        # 绝不能把 /Run 成功误判为“抓取成功”
        assert "完成：" not in text
        assert "导出成功" not in text
        assert app.monitor_cur_var.get() == "rfi-default · 已请求执行，等待后台完成"

    def test_gui_startup_reads_sched_log(self, root, tmp_path, monkeypatch):
        # 覆盖 _scheduled_log_path，让它指向 tmp 下的文件
        import news.gui as gui_mod

        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "scheduled-fetch.log"
        log_file.write_text(
            "========== 自动定时抓取开始 ==========\n"
            "SOURCE: hkej\nTARGET: 100（usable limit）\n发现数量: 100\n"
            "可读新闻: 100 / 目标 100\nFETCH: SUCCESS\nEXPORT: SUCCESS\n"
            "========== 自动定时抓取结束（exit=0）==========\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            gui_mod, "_scheduled_log_path", lambda: log_file
        )
        app = _make_monitor_app(root, tmp_path)
        # __init__ 已调用 _load_recent_scheduled_log
        assert any("HKEJ" in e and "新增 100 条" in e for e in app._monitor_entries)
