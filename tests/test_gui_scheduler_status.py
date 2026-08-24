"""GUI 定时任务「最终状态」显示逻辑测试。

覆盖需求第七/十三/十四/九条的核心：
- GUI 不应只根据 scheduler.json 的 enabled 判断状态，而要结合 Windows Task
  Scheduler 的真实状态（存在 / 启用 / 运行）。
- 状态合并为一个用户可见最终状态：运行中 / 未安装 / 已停用 / 安装失败 / 执行中 / 状态未知。
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
    assert app._get_scheduler_display_status(job) == "运行中"


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
    assert "1 个任务运行中" in text
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
    assert "2 个任务运行中" in text


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
    # 状态为「运行中」（已启用 + 任务存在并启用）
    assert app._get_scheduler_display_status(job) == "运行中"
