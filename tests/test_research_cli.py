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
    # 需求八：.doc（旧版 Word）必须一起支持
    assert set(SUPPORTED_EXTENSIONS) == {".pdf", ".docx", ".doc", ".html", ".htm"}
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
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_ROOT_PAGE_ID", raising=False)
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


def test_notion_sync_also_reports_empty_research_archive(monkeypatch, tmp_path):
    """统一入口在没有新研究报告时正常结束并给出明确状态。"""
    monkeypatch.delenv("RESEARCH_ARCHIVE_IN_SCHEDULER", raising=False)
    monkeypatch.setenv("RESEARCH_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("RESEARCH_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("RESEARCH_FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "db.sqlite"))
    from news.notion_sync import run_sync

    messages = run_sync(export_root=Path("/nonexistent-portable-root"), dry_run=True)
    assert "RESEARCH ARCHIVE · 研究报告：无新增/全部已去重" in messages


def test_cli_dry_run_logs_each_line_once(tmp_path, capsys):
    """需求九：单遍 CLI 日志 —— 每条信息只输出一次（无 logger + print 重复）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_html(inbox / "20260912 GENIMI 09696.HK 天齐锂业.html", title="天齐锂业研究")
    make_html(
        inbox / "20260912A CLAUDE 000660.KS SK海力士.html",
        title="SK海力士研究",
        body="由 Claude 生成的 SK海力士 半导体周期研究正文",
    )

    exit_code = main([
        "research-archive",
        "--inbox", str(inbox),
        "--archive", str(tmp_path / "archive"),
        "--failed", str(tmp_path / "failed"),
        "--state-db", str(tmp_path / "db.sqlite"),
        "--companies", str(tmp_path / "missing.json"),
    ])
    assert exit_code == 0

    out = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # 关键行恰好出现一次
    assert sum(1 for line in out if "发现文件：2" in line) == 1
    assert sum(1 for line in out if "模式：DRY-RUN" in line) == 1
    assert sum(1 for line in out if line.startswith("RESEARCH ARCHIVE START")) == 1
    assert sum(1 for line in out if line.startswith("RESEARCH ARCHIVE END")) == 1
    assert sum(1 for line in out if line.startswith("RESEARCH ARCHIVE SUMMARY")) == 1
    # 每个文件只列一次，且每个文件只有一条 sha256 行
    for name in ("天齐锂业", "SK海力士"):
        assert sum(1 for line in out if name in line and "[新增]" in line) == 1
    assert sum(1 for line in out if "sha256=" in line) == 2
    # 没有任何整行重复
    duplicates = {line for line in out if out.count(line) > 1}
    assert not duplicates, f"重复日志行：{duplicates}"


def test_cli_dry_run_shows_date_source(tmp_path, capsys):
    """需求七：dry-run 计划里必须显示 ``date=YYYY-MM-DD(source=filename)``。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_html(inbox / "20260912 GENIMI 09696.HK 天齐锂业.html")

    main([
        "research-archive",
        "--inbox", str(inbox),
        "--archive", str(tmp_path / "archive"),
        "--failed", str(tmp_path / "failed"),
        "--state-db", str(tmp_path / "db.sqlite"),
        "--companies", str(tmp_path / "missing.json"),
    ])
    out = capsys.readouterr().out
    assert "date=2026-09-12(source=filename)" in out
