from pathlib import Path

from news.scheduler_config import FREQ_DAILY, FREQ_HOURLY, NotionSyncSchedulerConfig
from news.task_scheduler import (
    build_notion_sync_arguments,
    build_notion_sync_schtasks_create,
    build_notion_sync_schtasks_run,
    delete_notion_sync_task,
    install_notion_sync_task,
    query_notion_sync_task,
    run_notion_sync_task,
)


def test_notion_task_name_and_action():
    cfg = NotionSyncSchedulerConfig()
    assert cfg.task_name() == "Laxinwen-Notion-Sync"
    assert build_notion_sync_arguments() == "-m news notion-sync"


def test_hourly_trigger_uses_minute_offset_and_project_root():
    cfg = NotionSyncSchedulerConfig(frequency=FREQ_HOURLY, minute_offset=10)
    cmd = build_notion_sync_schtasks_create(
        cfg, python_exe=r"C:\proj\.venv\Scripts\python.exe", project_root=r"D:\AIProjects\laxinwen"
    )
    joined = " ".join(cmd)
    assert "/SC HOURLY" in joined
    assert "/MO 1" in joined
    assert "/ST 00:10" in joined
    assert "D:\\AIProjects\\laxinwen" in joined
    assert "-m news notion-sync" in joined
    assert "scheduled-fetch" not in joined


def test_daily_trigger_uses_configured_time():
    cfg = NotionSyncSchedulerConfig(frequency=FREQ_DAILY, time="08:10")
    cmd = build_notion_sync_schtasks_create(cfg, python_exe="python", project_root=Path("D:/laxinwen"))
    joined = " ".join(cmd)
    assert "/SC DAILY" in joined
    assert "/ST 08:10" in joined


def test_task_operations_are_idempotent_and_use_schtasks(monkeypatch):
    calls = []
    monkeypatch.setattr("news.task_scheduler.is_windows", lambda: True)
    monkeypatch.setattr("news.task_scheduler.run_schtasks", lambda cmd: calls.append(cmd) or (0, "ok"))
    cfg = NotionSyncSchedulerConfig()

    assert install_notion_sync_task(cfg, project_root="D:/laxinwen")["ok"]
    assert install_notion_sync_task(cfg, project_root="D:/laxinwen")["ok"]
    assert delete_notion_sync_task(cfg)["ok"]
    assert run_notion_sync_task(cfg)["ok"]
    assert query_notion_sync_task(cfg)["ok"]

    assert sum("/Create" in cmd for cmd in calls) == 2
    assert any(cmd[:3] == ["schtasks", "/Run", "/TN"] for cmd in calls)
    assert any("/Query" in cmd for cmd in calls)
    assert all("Laxinwen-Notion-Sync" in cmd for cmd in calls)


def test_cli_supports_unified_notion_target():
    from news.cli import build_parser

    args = build_parser().parse_args(["scheduler", "install", "notion-sync", "--minute-offset", "10"])
    assert args.scheduler_action == "install"
    assert args.job_id == "notion-sync"
    assert args.minute_offset == 10


def test_cli_supports_legacy_alias():
    from news.cli import build_parser

    args = build_parser().parse_args(["scheduler", "run-notion-sync"])
    assert args.scheduler_action == "run-notion-sync"
