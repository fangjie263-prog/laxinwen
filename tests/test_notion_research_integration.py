from __future__ import annotations

from pathlib import Path


def _research_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("RESEARCH_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("RESEARCH_FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.sqlite"))


def test_notion_sync_runs_research_pipeline_in_dry_run(monkeypatch, tmp_path):
    _research_env(monkeypatch, tmp_path)

    from news.notion_sync import run_sync

    messages = run_sync(
        export_root=tmp_path / "portable",
        state_path=tmp_path / "notion.json",
        dry_run=True,
    )

    assert "RESEARCH ARCHIVE · 研究报告：无新增/全部已去重" in messages
    assert list((tmp_path / "archive").rglob("*")) == []


def test_research_failure_does_not_block_news_sync_and_reuses_client(monkeypatch, tmp_path):
    _research_env(monkeypatch, tmp_path)

    import news.notion_sync as notion_sync
    import news.research.pipeline as pipeline

    class FakeClient:
        instances = []

        def __init__(self, token, *, timeout):
            self.token = token
            self.timeout = timeout
            self.closed = False
            self.__class__.instances.append(self)

        def close(self):
            self.closed = True

    seen = {}

    def broken_research(**kwargs):
        seen["client"] = kwargs["client"]
        raise RuntimeError("research upload failed")

    monkeypatch.setattr(notion_sync, "NotionClient", FakeClient)
    monkeypatch.setattr(pipeline, "run_research_archive", broken_research)
    monkeypatch.setattr(
        notion_sync.NotionSync,
        "sync",
        lambda self, packages, **kwargs: ["SYNC SUCCESS · NEWS"],
    )

    messages = notion_sync.run_sync(
        token="token",
        root_page_id="root",
        export_root=tmp_path / "portable",
        state_path=tmp_path / "notion.json",
    )

    assert any(message.startswith("RESEARCH ARCHIVE FAILED") for message in messages)
    assert "SYNC SUCCESS · NEWS" in messages
    assert seen["client"] is FakeClient.instances[0]
    assert FakeClient.instances[0].closed


def test_notion_scheduler_uses_same_notion_sync_entrypoint(monkeypatch, tmp_path):
    calls = []

    import news.notion_sync as notion_sync
    from news.cli import main

    monkeypatch.setattr(notion_sync, "run_sync", lambda **kwargs: calls.append(kwargs) or [])
    assert main([
        "notion-sync", "--dry-run", "--export-root", str(tmp_path / "portable"),
        "--state", str(tmp_path / "state.json"),
    ]) == 0
    assert calls and calls[0]["dry_run"] is True
