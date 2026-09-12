"""CLI / 配置 / 调度钩子测试（对应需求四、五、二十八、三十一、三十七）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from news.cli import build_parser, main
from news.research.config import (
    IGNORED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    ResearchArchiveConfig,
    load_research_config,
)

from research_helpers import make_html


def test_cli_research_archive_defaults_to_dry_run():
    args = build_parser().parse_args(["research-archive"])
    assert args.apply is False
    assert args.dry_run is False
    assert args.func.__name__ == "cmd_research_archive"


def test_cli_research_archive_accepts_paths():
    args = build_parser().parse_args(
        [
            "research-archive", "--apply", "--inbox", "D:/AIResearchInbox",
            "--archive", "D:/Archive", "--failed", "D:/Failed",
            "--state-db", "D:/db.sqlite", "--companies", "D:/companies.json",
            "--limit", "5", "--recursive", "--retry-failed", "--keep-inbox",
        ]
    )
    assert args.apply is True
    assert args.inbox == "D:/AIResearchInbox"
    assert args.limit == 5
    assert args.recursive and args.retry_failed and args.keep_inbox


def test_cli_dry_run_flag_overrides_apply():
    args = build_parser().parse_args(["research-archive", "--apply", "--dry-run"])
    assert args.apply is True and args.dry_run is True


def test_cli_existing_subcommands_still_available():
    """确认原有子命令没有被破坏（需求三十六）。"""
    parser = build_parser()
    for command in ("notion-sync", "export", "fetch", "list", "status", "process", "scheduled-fetch"):
        assert parser.parse_args([command]).command == command
    # scheduler 需要位置参数，单独验证
    assert parser.parse_args(["scheduler", "status"]).command == "scheduler"


def test_cli_dry_run_end_to_end_produces_plan(tmp_path, capsys):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_html(inbox / "Claude 天齐锂业深度研究.html")
    code = main(
        [
            "research-archive",
            "--inbox", str(inbox),
            "--archive", str(tmp_path / "archive"),
            "--failed", str(tmp_path / "failed"),
            "--state-db", str(tmp_path / "db.sqlite"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY-RUN" in out
    assert "RESEARCH ARCHIVE SUMMARY" in out
    assert "09696.HK_天齐锂业" in out
    # dry-run 绝不移动文件
    assert (inbox / "Claude 天齐锂业深度研究.html").is_file()
    assert not list((tmp_path / "archive").rglob("*.html"))


def test_cli_apply_without_token_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_ROOT_PAGE_ID", raising=False)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_html(inbox / "Claude 天齐锂业深度研究.html")
    code = main(
        [
            "research-archive", "--apply",
            "--inbox", str(inbox),
            "--archive", str(tmp_path / "archive"),
            "--failed", str(tmp_path / "failed"),
            "--state-db", str(tmp_path / "db.sqlite"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "NOTION_TOKEN" in out
    # 原文件必须保留
    assert (inbox / "Claude 天齐锂业深度研究.html").is_file()


def test_load_config_defaults_to_dry_run(monkeypatch, tmp_path):
    monkeypatch.delenv("RESEARCH_DRY_RUN", raising=False)
    config = load_research_config(
        inbox_dir=tmp_path, archive_dir=tmp_path, failed_dir=tmp_path, db_path=tmp_path / "db"
    )
    assert config.dry_run is True
    assert config.max_upload_bytes == int(4.5 * 1024 * 1024)


def test_load_config_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("RESEARCH_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("RESEARCH_DRY_RUN", "false")
    monkeypatch.setenv("RESEARCH_MAX_UPLOAD_MB", "2")
    config = load_research_config()
    assert config.inbox_dir == tmp_path / "inbox"
    assert config.archive_dir == tmp_path / "archive"
    assert config.dry_run is False
    assert config.max_upload_bytes == 2 * 1024 * 1024


def test_explicit_dry_run_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_DRY_RUN", "false")
    config = load_research_config(dry_run=True)
    assert config.dry_run is True


def test_supported_and_ignored_extensions_do_not_overlap():
    assert set(SUPPORTED_EXTENSIONS) == {".pdf", ".docx", ".html", ".htm"}
    assert not set(SUPPORTED_EXTENSIONS) & set(IGNORED_EXTENSIONS)
    for ignored in (".txt", ".md", ".png", ".jpg"):
        assert ignored in IGNORED_EXTENSIONS


def test_config_is_supported_helper(tmp_path):
    config = ResearchArchiveConfig()
    assert config.is_supported("a.pdf")
    assert config.is_supported("a.HTM")
    assert not config.is_supported("a.txt")
    assert not config.is_supported("a.png")


def test_config_as_dict_and_ensure_dirs(tmp_path):
    config = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox", archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed", db_path=tmp_path / "db" / "x.sqlite",
        companies_file=None,
    )
    config.ensure_dirs()
    assert config.inbox_dir.is_dir() and config.archive_dir.is_dir()
    assert config.failed_dir.is_dir() and config.db_path.parent.is_dir()
    data = config.as_dict()
    assert data["inbox_dir"] == str(config.inbox_dir)
    assert ".pdf" in data["supported_extensions"]


# ---------- 与现有 Notion Sync / Scheduler 的接入 ----------

def test_notion_sync_hook_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RESEARCH_ARCHIVE_IN_SCHEDULER", raising=False)
    from news.notion_sync import _research_archive_enabled, run_research_archive_hook

    assert _research_archive_enabled() is False
    assert run_research_archive_hook() == []


def test_notion_sync_hook_enabled_runs_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_ARCHIVE_IN_SCHEDULER", "1")
    monkeypatch.setenv("RESEARCH_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("RESEARCH_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("RESEARCH_FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "db.sqlite"))
    (tmp_path / "inbox").mkdir()
    make_html(tmp_path / "inbox" / "Claude 天齐锂业研究.html")

    from news.notion_sync import run_research_archive_hook

    messages = run_research_archive_hook(dry_run=False)
    assert messages
    assert "RESEARCH ARCHIVE" in messages[0]
    # 没有 Notion 凭据 → 明确失败，但绝不移动文件（不静默“归档成功”）
    assert (tmp_path / "inbox" / "Claude 天齐锂业研究.html").is_file()


def test_notion_sync_hook_does_not_break_existing_run_sync(monkeypatch):
    """钩子关闭时 run_sync 的行为必须与原来完全一致。"""
    monkeypatch.delenv("RESEARCH_ARCHIVE_IN_SCHEDULER", raising=False)
    from news.notion_sync import run_sync

    # dry-run 且导出目录为空 → 没有消息，说明钩子没有额外输出
    messages = run_sync(export_root=Path("/nonexistent-portable-root"), dry_run=True)
    assert messages == []
