"""GUI 定时任务「最终状态」显示逻辑测试。

覆盖需求第七/十三/十四/九条的核心：
- GUI 不应只根据 scheduler.json 的 enabled 判断状态，而要结合 Windows Task
  Scheduler 的真实状态（存在 / 启用 / 运行）。
- 状态合并为一个用户可见最终状态：已启用 / 未安装 / 已停用 / 安装失败 / 执行中 / 状态未知。
- GUI 启动只读查询、不自动创建 Windows 任务。
- 底部汇总使用最终状态而非「已启用 x / 总数」。

通过注入假的 scheduler_query / scheduler_load_jobs 实现，无需 Windows。
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


def _job(enabled=True, job_id="rfi-hourly"):
    return SchedulerConfig(
        id=job_id,
        name=job_id,
        enabled=enabled,
        source="rfi",
        frequency="hourly",
        interval_hours=1,
        limit=10,
    )


def _make_app(root, tmp_path, jobs, query_side_effect):
    """构造带注入 scheduler 逻辑的 app。query_side_effect 是 scheduler_query 假实现。"""
    sched_file = tmp_path / "data" / "scheduler.json"

    def fake_query(job):
        return query_side_effect(job)

    def fake_save_jobs(job_list, path=None):
        return Path(path) if path else sched_file

    app = _NewsReaderApp(
        root,
        db_path=tmp_path / "gui.db",
        site="rfi",
        site_name="RFI",
        scheduler_load_jobs=lambda path=None: list(jobs),
        scheduler_save_jobs=fake_save_jobs,
        scheduler_query=fake_query,
        scheduler_config_path=sched_file,
    )
    return app


# ------------------------------------------------------------------ 状态计算


def test_parse_query_output_enabled_running(root, tmp_path):
    app = _make_app(root, tmp_path, [], lambda job: {})
    # 英文输出：任务存在且启用、正在运行
    out = (
        "TaskName: Laxinwen-RFI-rfi-hourly\n"
        "Scheduled Task State: Enabled\n"
        "Status: Running\n"
    )
    st = app._parse_task_query_output(out)
    assert st["exists"] is True
    assert st["enabled"] is True
    assert st["running"] is True


def test_parse_query_output_enabled_not_running(root, tmp_path):
    app = _make_app(root, tmp_path, [], lambda job: {})
    out = "Scheduled Task State: Enabled\nStatus: Ready\n"
    st = app._parse_task_query_output(out)
    assert st["exists"] is True
    assert st["enabled"] is True
    assert st["running"] is False


def test_parse_query_output_disabled(root, tmp_path):
    app = _make_app(root, tmp_path, [], lambda job: {})
    out = "Scheduled Task State: Disabled\n"
    st = app._parse_task_query_output(out)
    assert st["enabled"] is False


def test_parse_query_output_chinese_enabled(root, tmp_path):
    app = _make_app(root, tmp_path, [], lambda job: {})
    out = "计划任务状态: 已启用\n状态: 正在运行\n"
    st = app._parse_task_query_output(out)
    assert st["enabled"] is True
    assert st["running"] is True


def test_status_running_when_enabled_and_installed(root, tmp_path):
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "已启用"


def test_status_uninstalled_when_enabled_but_no_task(root, tmp_path):
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": False, "executed": True, "message": "ERROR: not found"},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "未安装"


def test_status_disabled_when_not_enabled(root, tmp_path):
    job = _job(enabled=False)
    app = _make_app(root, tmp_path, [job], lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"})
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "已停用"


def test_status_disabled_when_task_disabled(root, tmp_path):
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Disabled\n"},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "已停用"


def test_status_executing_when_task_running(root, tmp_path):
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\nStatus: Running\n"},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "执行中"


def test_status_unknown_when_query_not_executed(root, tmp_path):
    # 非 Windows：query_task 返回 executed=False（只生成了预览命令）
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": False, "message": "REQUIRES WINDOWS REAL TEST"},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "状态未知"


def test_status_install_failed_takes_priority(root, tmp_path):
    job = _job(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
    )
    app._refresh_windows_task_state()
    app._sched_install_failed.add(job.job_id)
    assert app._get_scheduler_display_status(job) == "安装失败"


# ------------------------------------------------------------------ 底部汇总


def test_bottom_summary_counts_final_states(root, tmp_path):
    running = _job(enabled=True, job_id="rfi-default")
    uninstalled = _job(enabled=True, job_id="rfi-hourly")
    disabled = _job(enabled=False, job_id="rfi-morning")

    def side_effect(job):
        if job.job_id == "rfi-default":
            return {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"}
        if job.job_id == "rfi-hourly":
            return {"ok": False, "executed": True, "message": "not found"}
        return {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"}

    app = _make_app(root, tmp_path, [running, uninstalled, disabled], side_effect)
    app._refresh_windows_task_state()
    app._refresh_sched_status()
    text = app.sched_status_var.get()
    assert "1 个任务已启用" in text
    assert "1 个未安装" in text
    assert "1 个已停用" in text


def test_bottom_summary_all_running(root, tmp_path):
    jobs = [_job(enabled=True, job_id="rfi-default"), _job(enabled=True, job_id="rfi-hourly")]
    app = _make_app(
        root, tmp_path, jobs,
        lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
    )
    app._refresh_windows_task_state()
    app._refresh_sched_status()
    text = app.sched_status_var.get()
    assert "2 个任务已启用" in text


# ------------------------------------------------------------------ 启动不自动安装


def test_startup_only_queries_does_not_install(root, tmp_path):
    """GUI 启动不应调用 scheduler_install，只读取状态。"""
    install_calls = []
    job = _job(enabled=True)

    sched_file = tmp_path / "data" / "scheduler.json"
    app = _NewsReaderApp(
        root,
        db_path=tmp_path / "gui.db",
        site="rfi",
        site_name="RFI",
        scheduler_load_jobs=lambda path=None: [job],
        scheduler_save_jobs=lambda jobs, path=None: sched_file,
        scheduler_query=lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
        scheduler_install=lambda j: install_calls.append(j) or {"ok": True, "executed": True},
        scheduler_config_path=sched_file,
    )
    app._refresh_windows_task_state()
    app._refresh_scheduler_table()
    assert install_calls == []
    # 状态为「已启用」（已启用 + 任务存在并启用）
    assert app._get_scheduler_display_status(job) == "已启用"


# ------------------------------------------------------------------ 启用 / 停用真正同步 Windows


def _make_app_ops(root, tmp_path, job, *, query, install=None, enable=None, disable=None,
                  run_now=None, save_jobs=None, load_jobs_list=None):
    """构造支持注入全部 scheduler 操作的 app，用于测试启用/停用/立即运行。"""
    sched_file = tmp_path / "data" / "scheduler.json"
    job_list = load_jobs_list if load_jobs_list is not None else [job]

    def fake_save(job_list, path=None):
        if save_jobs:
            save_jobs(job_list, path)
        return Path(path) if path else sched_file

    return _NewsReaderApp(
        root,
        db_path=tmp_path / "gui.db",
        site="rfi",
        site_name="RFI",
        scheduler_load_jobs=lambda path=None: job_list,
        scheduler_save_jobs=fake_save,
        scheduler_query=query,
        scheduler_install=install or (lambda j: {"ok": True, "executed": True, "task_name": j.task_name()}),
        scheduler_enable=enable or (lambda j: {"ok": True, "executed": True, "task_name": j.task_name()}),
        scheduler_disable=disable or (lambda j: {"ok": True, "executed": True, "task_name": j.task_name()}),
        scheduler_run_now=run_now or (lambda j: {"ok": True, "executed": True, "task_name": j.task_name()}),
        scheduler_config_path=sched_file,
    )


def _make_app_ops_selected(root, tmp_path, job, **kw):
    """构造 app 并预选中 job（等效于在任务列表中点击选中）。"""
    app = _make_app_ops(root, tmp_path, job, **kw)
    app._selected_job = job
    return app


def _pump(app, timeout=8.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and app._busy:
        app.root.update()
        time.sleep(0.02)
    app.root.update()


def test_enable_installs_when_task_missing(root, tmp_path):
    """启用：Windows 任务不存在 → 自动安装 → 最终已启用。"""
    install_calls = []
    job = _job(enabled=False)

    def query(j):
        # 先返回不存在，安装后返回已启用
        if install_calls:
            return {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"}
        return {"ok": False, "executed": True, "message": "not found"}

    app = _make_app_ops_selected(
        root, tmp_path, job, query=query,
        install=lambda j: install_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
    )
    app._sched_win_state[job.job_id] = {"exists": False, "enabled": False, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert job.enabled is True
    assert install_calls == [job.job_id]
    assert app._get_scheduler_display_status(job) == "已启用"


def test_enable_enables_existing_disabled_task(root, tmp_path):
    """启用：任务存在但 Disabled → 调用 enable，不调用 install。"""
    install_calls = []
    enable_calls = []
    job = _job(enabled=False)

    def query(j):
        # 存在但禁用 → 启用后变为 Enabled
        if enable_calls:
            return {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"}
        return {"ok": True, "executed": True, "message": "Scheduled Task State: Disabled\n"}

    app = _make_app_ops_selected(
        root, tmp_path, job, query=query,
        install=lambda j: install_calls.append(j.job_id) or {"ok": True, "executed": True},
        enable=lambda j: enable_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
    )
    app._sched_win_state[job.job_id] = {"exists": True, "enabled": False, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert job.enabled is True
    assert install_calls == []
    assert enable_calls == [job.job_id]
    assert app._get_scheduler_display_status(job) == "已启用"


def test_enable_failure_shows_install_failed(root, tmp_path):
    """启用：安装失败 → 状态「安装失败」，enabled 保持 true。"""
    job = _job(enabled=False)

    def query(j):
        return {"ok": False, "executed": True, "message": "not found"}

    app = _make_app_ops_selected(
        root, tmp_path, job, query=query,
        install=lambda j: {"ok": False, "executed": True, "message": "create failed"},
    )
    app._sched_win_state[job.job_id] = {"exists": False, "enabled": False, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert job.enabled is True
    assert app._get_scheduler_display_status(job) == "安装失败"


def test_disable_disables_windows_task(root, tmp_path):
    """停用：enabled=false + Windows 任务 Disabled。"""
    disable_calls = []
    job = _job(enabled=True)

    def query(j):
        if disable_calls:
            return {"ok": True, "executed": True, "message": "Scheduled Task State: Disabled\n"}
        return {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"}

    app = _make_app_ops_selected(
        root, tmp_path, job, query=query,
        disable=lambda j: disable_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
    )
    app._sched_win_state[job.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert job.enabled is False
    assert disable_calls == [job.job_id]
    assert app._get_scheduler_display_status(job) == "已停用"


def test_disable_no_task_skips_disable(root, tmp_path):
    """停用：Windows 任务不存在 → 不调用 disable，enabled=false。"""
    disable_calls = []
    job = _job(enabled=True)
    app = _make_app_ops_selected(
        root, tmp_path, job, query=lambda j: {"ok": False, "executed": True, "message": "not found"},
        disable=lambda j: disable_calls.append(j.job_id) or {"ok": True, "executed": True},
    )
    app._sched_win_state[job.job_id] = {"exists": False, "enabled": False, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert job.enabled is False
    assert disable_calls == []
    assert app._get_scheduler_display_status(job) == "已停用"


def test_disabled_job_cannot_run_now(root, tmp_path):
    """停用任务不能立即运行：不调用 schtasks /Run。"""
    run_now_calls = []
    job = _job(enabled=False)
    app = _make_app_ops_selected(
        root, tmp_path, job, query=lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
        run_now=lambda j: run_now_calls.append(j.job_id) or {"ok": True, "executed": True},
    )
    app._sched_win_state[job.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_run_now()
    _pump(app)
    assert run_now_calls == []
    log = app.log_text.get("1.0", "end")
    assert "任务已停用，请先启用任务" in log


def test_enabled_job_run_now_uses_schtasks_run(root, tmp_path):
    """启用任务立即运行：调用 schtasks /Run（scheduler_run_now）。"""
    run_now_calls = []
    job = _job(enabled=True)
    app = _make_app_ops_selected(
        root, tmp_path, job,
        query=lambda j: {"ok": True, "executed": True, "message": "Scheduled Task State: Enabled\n"},
        run_now=lambda j: run_now_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
    )
    app._sched_win_state[job.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_run_now()
    _pump(app)
    assert run_now_calls == [job.job_id]
    log = app.log_text.get("1.0", "end")
    assert "已请求 Windows Task Scheduler 执行任务" in log


# ------------------------------------------------------------------ 实时抓取日志 / 状态摘要 / 后台日志


def test_manual_fetch_logs_fetch_and_export_status(root, tmp_path, monkeypatch):
    """手动抓取日志包含发现/重复/可读/FETCH/EXPORT，且不阻塞。"""
    import time as _time

    from tests.test_gui import FakePipeline, FakeStats, _make_app as _make_gui_app

    app, ctx = _make_gui_app(root, tmp_path)
    app.site_combo.set("rfi")
    app._on_source_changed()
    app.limit_var.set("10")
    app._on_fetch()
    deadline = _time.monotonic() + 8
    while _time.monotonic() < deadline and app._busy:
        app.root.update()
        _time.sleep(0.02)
    app.root.update()
    log = app.log_text.get("1.0", "end")
    assert "FETCH: SUCCESS" in log
    assert "EXPORT: SUCCESS" in log
    assert "发现" in log
    assert "重复" in log
    assert "可读" in log
    # 状态摘要已更新
    assert "已完成" in app.fetch_status_var.get()


def test_manual_fetch_no_articles_is_success(root, tmp_path):
    """没有新文章：usable=0 仍显示 FETCH: SUCCESS，不显示 FAILED。"""
    import time as _time

    from tests.test_gui import FakePipeline, FakeStats, _make_app as _make_gui_app

    app, ctx = _make_gui_app(root, tmp_path, pipeline_stats=FakeStats(discovered=0, usable=0))
    app.site_combo.set("rfi")
    app._on_source_changed()
    app.limit_var.set("10")
    app._on_fetch()
    deadline = _time.monotonic() + 8
    while _time.monotonic() < deadline and app._busy:
        app.root.update()
        _time.sleep(0.02)
    app.root.update()
    log = app.log_text.get("1.0", "end")
    assert "FETCH: SUCCESS" in log
    assert "FETCH: FAILED" not in log
    assert "没有发现新的可读新闻" in log


def test_log_clear_only_clears_gui_display(root, tmp_path, monkeypatch):
    """清空当前显示：只清 GUI 日志框，不删除真实日志文件。"""
    import time as _time

    log_file = tmp_path / "logs" / "scheduled-fetch.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("JOB: test\nSOURCE: rfi\nFETCH: SUCCESS\n", encoding="utf-8")
    monkeypatch.setattr("news.gui._scheduled_log_path", lambda: log_file)

    from tests.test_gui import _make_app as _make_gui_app

    app, _ctx = _make_gui_app(root, tmp_path)
    app._load_recent_scheduled_log()
    assert app.log_text.get("1.0", "end").strip() != ""
    app._on_log_clear()
    assert app.log_text.get("1.0", "end").strip() == ""
    # 真实日志文件仍然存在、内容未删
    assert log_file.read_text(encoding="utf-8") == "JOB: test\nSOURCE: rfi\nFETCH: SUCCESS\n"


def test_startup_loads_recent_scheduled_log(root, tmp_path, monkeypatch):
    """GUI 启动能读取最近 scheduled-fetch.log 并显示（不删除真实文件）。"""
    log_file = tmp_path / "logs" / "scheduled-fetch.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "SOURCE: rfi\n"
        "TARGET: 10\n"
        "发现数量: 7\n"
        "可读新闻: 5 / 目标 10\n"
        "FETCH: SUCCESS\n"
        "EXPORT: SUCCESS\n"
    )
    log_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr("news.gui._scheduled_log_path", lambda: log_file)

    from tests.test_gui import _make_app as _make_gui_app

    app, _ctx = _make_gui_app(root, tmp_path)
    log = app.log_text.get("1.0", "end")
    assert "SOURCE: rfi" in log
    assert "FETCH: SUCCESS" in log
    # 状态摘要更新为“已完成”，且可读=5
    assert "已完成" in app.fetch_status_var.get()
    assert log_file.exists()  # 真实日志未删除


def test_fetch_log_does_not_block_tkinter(root, tmp_path):
    """抓取过程中 GUI 不冻结：worker 线程通过 queue 传递日志。"""
    import time as _time

    from tests.test_gui import FakeStats, _make_app as _make_gui_app

    app, ctx = _make_gui_app(root, tmp_path, pipeline_stats=FakeStats(discovered=5, usable=3))
    app.site_combo.set("rfi")
    app._on_source_changed()
    app.limit_var.set("5")
    # 手动驱动事件循环多次，确保主线程持续响应（不抛异常）
    app._on_fetch()
    for _ in range(20):
        app.root.update()
        _time.sleep(0.01)
    assert True


# --------------------------------------------------------------------------
# 真实 Windows 输出对齐空格回归测试（schtasks /V /FO LIST 用多空格对齐字段与值）
# --------------------------------------------------------------------------
# 根因：真实 schtasks 输出形如 "Scheduled Task State:                    Enabled"，
# 字段名与值之间是多个对齐空格；旧解析器按单个空格做子串匹配导致漏判 enabled，
# 从而把本应「已启用」的任务误判为「已停用」。下面用带对齐空格的输出验证修复。
# --------------------------------------------------------------------------


def _job_daily(job_id="rfi-default", enabled=True):
    return SchedulerConfig(
        id=job_id,
        name=job_id,
        enabled=enabled,
        source="rfi" if job_id.startswith("rfi") else "hkej",
        frequency="daily",
        time="08:00",
        limit=50,
    )


# 模拟真实 Windows 列对齐输出：字段名与值之间填充多空格
REAL_ALIGNED_ENABLED_READY = (
    "TaskName:                                \\Laxinwen-RFI-rfi-default\n"
    "Next Run Time:                           2026/8/25 8:00:00\n"
    "Status:                                  Ready\n"
    "Scheduled Task State:                    Enabled\n"
    "Task To Run:                             cmd.exe /c \"\"...\"\"\n"
)
REAL_ALIGNED_ENABLED_RUNNING = (
    "TaskName:                                \\Laxinwen-RFI-rfi-default\n"
    "Status:                                  Running\n"
    "Scheduled Task State:                    Enabled\n"
)
REAL_ALIGNED_DISABLED = (
    "TaskName:                                \\Laxinwen-RFI-rfi-default\n"
    "Scheduled Task State:                    Disabled\n"
)
REAL_ALIGNED_CN_ENABLED = "计划任务状态:                    已启用\n状态:                        正在运行\n"


def test_real_aligned_output_enabled_ready(root, tmp_path):
    """需求1：enabled=true + Windows State=Enabled + Status=Ready → 已启用。

    即“Status: Ready 代表当前未运行，不代表 Disabled”，绝不能显示“已停用”。
    """
    job = _job_daily(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_READY},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "已启用"


def test_real_aligned_output_enabled_running(root, tmp_path):
    """需求2：enabled=true + Windows State=Enabled + Status=Running → 执行中。"""
    job = _job_daily(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_RUNNING},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "执行中"


def test_real_aligned_output_disabled_task(root, tmp_path):
    """Windows 任务 Disabled（对齐空格）→ 已停用。"""
    job = _job_daily(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": REAL_ALIGNED_DISABLED},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "已停用"


def test_real_aligned_output_chinese_enabled(root, tmp_path):
    """中文 Windows 对齐输出 → 已启用 / 执行中。"""
    job = _job_daily(enabled=True)
    app = _make_app(
        root, tmp_path, [job],
        lambda j: {"ok": True, "executed": True, "message": REAL_ALIGNED_CN_ENABLED},
    )
    app._refresh_windows_task_state()
    assert app._get_scheduler_display_status(job) == "执行中"


# ------------------------------------------------------------------ 启用 / 停用端到端（真实对齐输出）


def test_enable_flow_treeview_and_bottom_summary(root, tmp_path):
    """需求4：点击启用后 Treeview=已启用，底部=1 个任务已启用（真实对齐输出）。"""
    install_calls = []
    rfi = _job_daily("rfi-default", enabled=False)
    hkej = _job_daily("hkej-default", enabled=False)

    def query(j):
        if j.job_id == "rfi-default":
            return {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_READY}
        return {"ok": True, "executed": True, "message": REAL_ALIGNED_DISABLED}

    app = _make_app_ops_selected(
        root, tmp_path, rfi, query=query,
        install=lambda j: install_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
        load_jobs_list=[rfi, hkej],
    )
    app._sched_win_state[rfi.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_toggle()
    _pump(app)
    # scheduler.json enabled=true
    assert rfi.enabled is True
    # Treeview 显示已启用
    values = dict((app.sched_tree.item(i)["values"][0], app.sched_tree.item(i)["values"][4])
                  for i in app.sched_tree.get_children())
    assert values["rfi-default"] == "已启用"
    # 底部汇总 1 个已启用
    assert "1 个任务已启用" in app.sched_status_var.get()


def test_disable_flow_treeview_and_bottom_summary(root, tmp_path):
    """需求5：点击停用后 scheduler.json=false、Windows Disabled、Treeview=已停用、底部正确。"""
    disable_calls = []
    rfi = _job_daily("rfi-default", enabled=True)
    hkej = _job_daily("hkej-default", enabled=False)

    def query(j):
        if j.job_id == "rfi-default":
            if disable_calls:
                return {"ok": True, "executed": True, "message": REAL_ALIGNED_DISABLED}
            return {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_READY}
        return {"ok": True, "executed": True, "message": REAL_ALIGNED_DISABLED}

    app = _make_app_ops_selected(
        root, tmp_path, rfi, query=query,
        disable=lambda j: disable_calls.append(j.job_id) or {"ok": True, "executed": True, "task_name": j.task_name()},
        load_jobs_list=[rfi, hkej],
    )
    app._sched_win_state[rfi.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert rfi.enabled is False
    assert disable_calls == [rfi.job_id]
    values = dict((app.sched_tree.item(i)["values"][0], app.sched_tree.item(i)["values"][4])
                  for i in app.sched_tree.get_children())
    assert values["rfi-default"] == "已停用"
    # rfi 停用、hkej 停用 → 0 个已启用
    assert "0 个任务已启用" in app.sched_status_var.get()


def test_enable_does_not_revert_on_refresh(root, tmp_path):
    """需求6：启用后 refresh/reload 不得重新显示“已停用”。"""
    rfi = _job_daily("rfi-default", enabled=False)
    hkej = _job_daily("hkej-default", enabled=False)

    def query(j):
        return {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_READY}

    app = _make_app_ops_selected(root, tmp_path, rfi, query=query, load_jobs_list=[rfi, hkej])
    app._sched_win_state[rfi.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_toggle()
    _pump(app)
    assert rfi.enabled is True
    # 模拟 reload / 重新查询：仅查询状态，不改变 enabled
    app._refresh_windows_task_state()
    app._refresh_scheduler_table()
    assert app._get_scheduler_display_status(rfi) == "已启用"
    values = dict((app.sched_tree.item(i)["values"][0], app.sched_tree.item(i)["values"][4])
                  for i in app.sched_tree.get_children())
    assert values["rfi-default"] == "已启用"


def test_toggle_only_affects_selected_job(root, tmp_path):
    """需求7：多任务下只改变当前 job，不影响其它 job。"""
    rfi = _job_daily("rfi-default", enabled=False)
    hkej = _job_daily("hkej-default", enabled=False)

    def query(j):
        if j.job_id == "rfi-default":
            return {"ok": True, "executed": True, "message": REAL_ALIGNED_ENABLED_READY}
        return {"ok": True, "executed": True, "message": REAL_ALIGNED_DISABLED}

    app = _make_app_ops_selected(root, tmp_path, rfi, query=query, load_jobs_list=[rfi, hkej])
    app._sched_win_state[rfi.job_id] = {"exists": True, "enabled": True, "running": False}
    app._on_sched_toggle()
    _pump(app)
    # 只有被选中的 rfi 被启用
    assert rfi.enabled is True
    assert hkej.enabled is False
