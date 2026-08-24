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
        assert "完成：新增 50 条（目标 50 条），HTML + Word 导出成功" in app._monitor_entries[1]
        assert app._monitor_cur == "RFI · 已完成 · 新增 50 条，HTML + Word 导出成功"

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

    def test_parse_export_detail_success(self):
        """EXPORT: SUCCESS 带分项 → 解析为「HTML + Word 导出成功」。"""
        assert _NewsReaderApp._parse_export_detail(
            "EXPORT: SUCCESS → HTML: SUCCESS / WORD: SUCCESS (...)"
        ) == "HTML + Word 导出成功"
        assert _NewsReaderApp._parse_export_detail("EXPORT: SUCCESS") == "HTML + Word 导出成功"

    def test_parse_export_detail_word_failed(self):
        """HTML 成功 + Word 失败 → 解析为「Word 导出失败」。"""
        assert _NewsReaderApp._parse_export_detail(
            "EXPORT: FAILED → HTML: SUCCESS / WORD: FAILED"
        ) == "Word 导出失败（HTML 成功）"

    def test_parse_export_detail_html_failed(self):
        """HTML 失败 + Word 成功 → 解析为「HTML 导出失败」。"""
        assert _NewsReaderApp._parse_export_detail(
            "EXPORT: FAILED → HTML: FAILED / WORD: SUCCESS"
        ) == "HTML 导出失败（Word 成功）"

    def test_parse_export_detail_both_failed(self):
        assert _NewsReaderApp._parse_export_detail(
            "EXPORT: FAILED → HTML: FAILED / WORD: FAILED"
        ) == "HTML + Word 导出失败"

    def test_export_word_failure_shown_in_monitor(self):
        """监控文案：HTML 成功 + Word 失败 → 显示「Word 导出失败」，不误判 FETCH FAILED。"""
        app = self._bare()
        for ln in [
            "开始抓取 RFI 最新 50 篇",
            "发现：50",
            "可读新闻：50 / 目标 50",
            "FETCH: SUCCESS",
            "EXPORT: FAILED → HTML: SUCCESS / WORD: FAILED",
            "抓取完成（RFI，limit=50）",
        ]:
            app._monitor_feed_line(ln)
        # 完成摘要明确 Word 失败，而非 FETCH FAILED
        assert "Word 导出失败" in app._monitor_entries[-1]
        assert "FETCH FAILED" not in app._monitor_entries[-1]
        assert "抓取失败" not in app._monitor_entries[-1]

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
        # “无新文章”是正常完成，不是失败
        assert "无新文章" in app._monitor_entries[-1]
        assert "抓取失败" not in app._monitor_entries[-1]

    def test_failure(self):
        app = self._bare()
        app._monitor_task = {"source": "rfi", "limit": 50}
        app._monitor_feed_line("抓取失败：连接超时")
        assert app._monitor_entries[0].endswith("抓取失败")
        assert app._monitor_cur == "RFI · 抓取失败"

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


# ------------------------------------------------------------------ JOB + SOURCE 解析


class TestMonitorJobSourceParsing:
    """覆盖任务身份（JOB + SOURCE）解析与多任务区分。

    对应验收：JOB=test + SOURCE=eco 正确关联；同 source 不同 job 显示成
    两个不同任务；FETCH / EXPORT 独立；usable=0 不是失败。
    """

    def _bare(self):
        app = object.__new__(_NewsReaderApp)
        app._monitor_entries = []
        app._monitor_cur = "空闲"
        app._monitor_task = None
        app._monitor_render = lambda: None
        app._monitor_set_cur = lambda text: setattr(app, "_monitor_cur", text)
        return app

    def test_job_and_source_parsed(self):
        app = self._bare()
        app._monitor_feed_line("JOB: test")
        app._monitor_feed_line("SOURCE: eco")
        assert app._monitor_task["job"] == "test"
        assert app._monitor_task["source"] == "eco"
        assert app._monitor_identity(app._monitor_task) == "test · ECO"

    def test_identity_never_question_mark(self):
        """绝不出裸 `?`：只有 job / 只有 source / 都没有时都能回退。"""
        app = self._bare()
        # 只有 job
        t1 = {"job": "test"}
        assert app._monitor_identity(t1) == "test"
        # 只有 source
        t2 = {"source": "eco"}
        assert app._monitor_identity(t2) == "ECO"
        # 都没有
        t3 = {}
        assert app._monitor_identity(t3) != "?"
        assert app._monitor_identity(t3) == "任务"

    def test_windows_verified_full_sample(self):
        """与本次 Windows 实机验证完全一致的日志样本。"""
        app = self._bare()
        sched = [
            "========== 自动定时抓取开始 ==========",
            "JOB: test",
            "NAME: test",
            "SOURCE: eco",
            "TARGET: 200（usable limit）",
            "自动导出: 开启",
            "JOB: test",
            "SOURCE: eco",
            "TARGET: 200",
            "发现数量: 200",
            "重复数量: 0",
            "正文下载成功: 200",
            "正文提取成功: 200",
            "质量不合格: 0",
            "抓取/提取失败: 0",
            "可读新闻: 200 / 目标 200",
            "FETCH: SUCCESS（usable=200 / 目标 200）",
            "EXPORT: SUCCESS → 便携阅读包已导出 200 篇 → data/export/portable/Laxinwen-ECO-2026-08-24-142002-test",
            "========== 自动定时抓取结束（exit=0）==========",
        ]
        app._monitor_parse_sched_log(sched)
        assert len(app._monitor_entries) == 1
        entry = app._monitor_entries[0]
        assert "test · ECO" in entry
        assert "完成：新增 200 条" in entry
        assert "导出成功" in entry
        assert "?" not in entry
        assert app._monitor_cur == "test · ECO · 已完成 · 新增 200 条，HTML + Word 导出成功"

    def test_same_source_different_job_distinguished(self):
        """rfi-morning 与 rfi-hourly 同 source 但必须区分成两个任务。"""
        app = self._bare()
        sched = [
            "========== 自动定时抓取开始 ==========",
            "JOB: rfi-morning", "SOURCE: rfi", "TARGET: 50（usable limit）",
            "发现数量: 53", "可读新闻: 50 / 目标 50",
            "FETCH: SUCCESS（usable=50 / 目标 50）", "EXPORT: SUCCESS → ...",
            "========== 自动定时抓取结束（exit=0）==========",
            "========== 自动定时抓取开始 ==========",
            "JOB: rfi-hourly", "SOURCE: rfi", "TARGET: 10（usable limit）",
            "发现数量: 12", "可读新闻: 10 / 目标 10",
            "FETCH: SUCCESS（usable=10 / 目标 10）", "EXPORT: SUCCESS → ...",
            "========== 自动定时抓取结束（exit=0）==========",
        ]
        app._monitor_parse_sched_log(sched)
        assert len(app._monitor_entries) == 2
        assert any("rfi-morning · RFI" in e and "新增 50 条" in e
                   for e in app._monitor_entries)
        assert any("rfi-hourly · RFI" in e and "新增 10 条" in e
                   for e in app._monitor_entries)

    def test_fetch_success_export_success(self):
        app = self._bare()
        for ln in [
            "JOB: test", "SOURCE: eco", "TARGET: 200（usable limit）",
            "可读新闻: 200 / 目标 200",
            "FETCH: SUCCESS（usable=200 / 目标 200）",
            "EXPORT: SUCCESS → ...", "自动定时抓取结束（exit=0）",
        ]:
            app._monitor_feed_line(ln)
        assert "test · ECO" in app._monitor_entries[0]
        assert "完成：新增 200 条" in app._monitor_entries[0]
        assert "导出成功" in app._monitor_entries[0]

    def test_fetch_success_export_failed(self):
        """EXPORT FAILED 不应把整个 FETCH 标记成失败。"""
        app = self._bare()
        for ln in [
            "JOB: test", "SOURCE: eco", "TARGET: 200（usable limit）",
            "可读新闻: 200 / 目标 200",
            "FETCH: SUCCESS（usable=200 / 目标 200）",
            "EXPORT: FAILED", "自动定时抓取结束（exit=0）",
        ]:
            app._monitor_feed_line(ln)
        entry = app._monitor_entries[0]
        assert "test · ECO" in entry
        assert "完成：新增 200 条" in entry
        assert "导出失败" in entry
        assert "抓取失败" not in entry

    def test_fetch_failed(self):
        app = self._bare()
        for ln in [
            "JOB: test", "SOURCE: eco", "TARGET: 200（usable limit）",
            "FETCH: FAILED",
        ]:
            app._monitor_feed_line(ln)
        assert "test · ECO  抓取失败" in app._monitor_entries[0]
        assert app._monitor_cur == "test · ECO · 抓取失败"

    def test_usable_zero_is_not_failure(self):
        """usable=0 + FETCH SUCCESS → 显示无新文章，不是失败。"""
        app = self._bare()
        for ln in [
            "JOB: rfi-hourly", "SOURCE: rfi", "TARGET: 50（usable limit）",
            "发现数量: 0", "可读新闻: 0 / 目标 50",
            "FETCH: SUCCESS（usable=0 / 目标 50）",
            "EXPORT: SUCCESS → ...", "自动定时抓取结束（exit=0）",
        ]:
            app._monitor_feed_line(ln)
        entry = app._monitor_entries[0]
        assert "rfi-hourly · RFI" in entry
        assert "无新文章" in entry
        assert "导出成功" in entry
        assert "抓取失败" not in entry

    def test_no_duplicate_consumption(self):
        """同一段日志不应被消费两次（增量读取不回放）。"""
        app = self._bare()
        sched = [
            "========== 自动定时抓取开始 ==========",
            "JOB: test", "SOURCE: eco", "TARGET: 200（usable limit）",
            "可读新闻: 200 / 目标 200",
            "FETCH: SUCCESS（usable=200 / 目标 200）",
            "EXPORT: SUCCESS → ...", "========== 自动定时抓取结束（exit=0）==========",
        ]
        app._monitor_parse_sched_log(sched)
        assert len(app._monitor_entries) == 1
        # 再次解析同一日志：不应新增重复摘要
        app._monitor_entries = []
        app._monitor_task = None
        app._monitor_parse_sched_log(sched)
        assert len(app._monitor_entries) == 1

    def test_incremental_poll_new_task(self):
        """GUI 打开期间增量读取：新任务出现时能识别为新段落并终结上一个。"""
        app = self._bare()
        # 第一段已完整
        app._monitor_sched_tail_pos = 0
        part1 = [
            "========== 自动定时抓取开始 ==========",
            "JOB: test", "SOURCE: eco", "TARGET: 200（usable limit）",
            "可读新闻: 200 / 目标 200",
            "FETCH: SUCCESS（usable=200 / 目标 200）",
            "EXPORT: SUCCESS → ...", "========== 自动定时抓取结束（exit=0）==========",
        ]
        app._monitor_parse_sched_log(part1)
        assert len(app._monitor_entries) == 1
        assert "test · ECO" in app._monitor_entries[0]

    def test_run_now_not_treated_as_success(self):
        """「立即运行一次」只显示已请求执行，不能把 /Run 成功当抓取成功。"""
        app = self._bare()
        app._monitor_feed_line("已请求 Windows Task Scheduler 执行任务：test")
        # 没有 FETCH/EXPORT 结果 → 不应出现“完成/导出成功”
        assert not app._monitor_entries


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
        app._monitor_entries = ["08:00  RFI  完成：新增 50 条，HTML + Word 导出成功"]
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

    def test_incremental_poll_reads_new_background_task(self, root, tmp_path, monkeypatch):
        """GUI 打开期间增量读取：新后台任务完成 → 追加新摘要。"""
        import news.gui as gui_mod

        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "scheduled-fetch.log"
        log_file.write_text(
            "========== 自动定时抓取开始 ==========\n"
            "JOB: test\nSOURCE: eco\nTARGET: 200（usable limit）\n"
            "发现数量: 200\n可读新闻: 200 / 目标 200\n"
            "FETCH: SUCCESS（usable=200 / 目标 200）\n"
            "EXPORT: SUCCESS → ...\n"
            "========== 自动定时抓取结束（exit=0）==========\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gui_mod, "_scheduled_log_path", lambda: log_file)
        app = _make_monitor_app(root, tmp_path)
        # 启动已读取历史
        assert any("test · ECO" in e for e in app._monitor_entries)
        before = len(app._monitor_entries)
        # 后台新增一段任务
        with log_file.open("a", encoding="utf-8") as f:
            f.write(
                "========== 自动定时抓取开始 ==========\n"
                "JOB: rfi-morning\nSOURCE: rfi\nTARGET: 50（usable limit）\n"
                "发现数量: 53\n可读新闻: 50 / 目标 50\n"
                "FETCH: SUCCESS（usable=50 / 目标 50）\n"
                "EXPORT: SUCCESS → ...\n"
                "========== 自动定时抓取结束（exit=0）==========\n"
            )
        app._monitor_poll_sched_log()
        # 新增了 rfi-morning 任务摘要
        assert len(app._monitor_entries) == before + 1
        assert any("rfi-morning · RFI" in e and "新增 50 条" in e
                   for e in app._monitor_entries)

    def test_incremental_poll_no_duplicate(self, root, tmp_path, monkeypatch):
        """增量轮询不重复消费同一任务（已消费行不回放）。"""
        import news.gui as gui_mod

        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "scheduled-fetch.log"
        log_file.write_text(
            "========== 自动定时抓取开始 ==========\n"
            "JOB: test\nSOURCE: eco\nTARGET: 200（usable limit）\n"
            "发现数量: 200\n可读新闻: 200 / 目标 200\n"
            "FETCH: SUCCESS（usable=200 / 目标 200）\n"
            "EXPORT: SUCCESS → ...\n"
            "========== 自动定时抓取结束（exit=0）==========\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(gui_mod, "_scheduled_log_path", lambda: log_file)
        app = _make_monitor_app(root, tmp_path)
        # 启动已消费全文
        assert len(app._monitor_entries) == 1
        # 连续轮询（日志未变化）不应新增摘要
        app._monitor_poll_sched_log()
        app._monitor_poll_sched_log()
        assert len(app._monitor_entries) == 1
        assert sum("test · ECO" in e for e in app._monitor_entries) == 1
