"""Notion 集成测试（对应需求二十四、二十八、二十九、三十七）。

全部使用 mock 客户端，覆盖：首次上传 / 重复上传 / 上传失败 / 恢复。
不做真实 Notion API 调用。
"""

from __future__ import annotations

import pytest

from news.research.archive_store import (
    STATUS_FAILED,
    STATUS_SPLIT,
    STATUS_UPLOADED,
    ResearchArchiveStore,
    ResearchFileRecord,
)
from news.research.config import ResearchArchiveConfig
from news.research.notion_archive import ResearchNotionArchiver, build_archiver
from news.research.pipeline import run_research_archive

from research_helpers import make_html


class RecordingNotion:
    def __init__(self, *, fail_times: int = 0, fail_page_lookup: bool = False):
        self.pages: dict[str, dict[str, dict]] = {}
        self.blocks: dict[str, list] = {}
        self.uploads: list[str] = []
        self.counter = 0
        self.fail_times = fail_times
        self.fail_page_lookup = fail_page_lookup
        self.retrieve_page_calls = 0

    def retrieve_page(self, page_id):
        self.retrieve_page_calls += 1
        if self.fail_page_lookup:
            raise RuntimeError("NOTION_ROOT_PAGE_ID 无效")
        return {"id": page_id}

    def child_pages(self, parent_id):
        return list(self.pages.get(parent_id, {}).values())

    def find_or_create_child_page(self, parent_id, title, position=None):
        children = self.pages.setdefault(parent_id, {})
        if title in children:
            return children[title]["id"]
        self.counter += 1
        page_id = f"page{self.counter}"
        children[title] = {"id": page_id, "title": title}
        self.pages.setdefault(page_id, {})
        return page_id

    def date_page_position(self, parent_id, date):
        return {"type": "page_start"}

    def upload_file(self, path, content_type=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("上传失败（模拟）")
        self.uploads.append(path.name)
        self.counter += 1
        return f"upload{self.counter}"

    def append_blocks(self, page_id, blocks):
        self.blocks.setdefault(page_id, []).extend(blocks)

    def close(self):
        pass


@pytest.fixture()
def config(tmp_path):
    cfg = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "research.db",
        companies_file=None,
        dry_run=False,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def store(config):
    with ResearchArchiveStore(config.db_path) as instance:
        yield instance


def test_first_upload_creates_date_and_company_pages(config, store):
    fake = RecordingNotion()
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok
    titles = {title for children in fake.pages.values() for title in children}
    assert "2026-09-12" in titles or any("2026-09-12" in t for t in titles)
    assert "09696.HK_天齐锂业" in titles
    assert fake.retrieve_page_calls == 1


def test_duplicate_upload_never_called_twice(config, store):
    fake = RecordingNotion()
    content = "<html><head><title>Claude 天齐锂业</title></head><body>研究</body></html>"
    (config.inbox_dir / "Claude 天齐锂业深度研究.html").write_text(content, encoding="utf-8")
    archiver = ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store)
    run_research_archive(config=config, store=store, archiver=archiver)
    first_uploads = list(fake.uploads)

    (config.inbox_dir / "Claude 天齐锂业深度研究最终版.html").write_text(content, encoding="utf-8")
    second = run_research_archive(config=config, store=store, archiver=archiver)
    assert second.duplicates == 1
    assert fake.uploads == first_uploads


def test_upload_failure_records_error_and_does_not_mark_uploaded(config, store):
    fake = RecordingNotion(fail_times=1)
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.failed == 1
    record = store.list_records()[0]
    assert record.status == STATUS_FAILED
    assert not record.uploaded
    assert "上传失败" in record.error


def test_upload_failure_recovery_after_retry(config, store):
    fake = RecordingNotion(fail_times=1)
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    archiver = ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store)
    run_research_archive(config=config, store=store, archiver=archiver)

    import shutil

    failed_file = next(config.failed_dir.rglob("*.html"))
    shutil.move(str(failed_file), str(config.inbox_dir / failed_file.name))
    recovered = run_research_archive(
        config=config, store=store, archiver=archiver, retry_failed=True
    )
    assert recovered.uploaded == 1
    record = store.list_records()[0]
    assert record.status == STATUS_UPLOADED
    assert record.notion_page_id
    assert record.uploaded_at


def test_should_upload_logic(store, config):
    archiver = ResearchNotionArchiver(config, client=RecordingNotion(), root_page_id="ROOT", store=store)
    fresh = ResearchFileRecord(sha256="new", status="NEW")
    assert archiver.should_upload(fresh)[0] is True

    store.upsert(ResearchFileRecord(sha256="done"))
    store.mark("done", status=STATUS_UPLOADED, notion_page_id="page-x")
    done = store.find_by_sha256("done")
    assert archiver.should_upload(done)[0] is False
    assert "跳过" in archiver.should_upload(done)[1]

    store.upsert(ResearchFileRecord(sha256="bad"))
    store.mark_failed("bad", "boom")
    failed = store.find_by_sha256("bad")
    assert archiver.should_upload(failed)[0] is False
    assert archiver.should_upload(failed, retry_failed=True)[0] is True


def test_can_upload_requires_client_and_root(config, store):
    assert not ResearchNotionArchiver(config, client=None, root_page_id="ROOT", store=store).can_upload
    assert not ResearchNotionArchiver(config, client=RecordingNotion(), root_page_id="", store=store).can_upload
    assert ResearchNotionArchiver(config, client=RecordingNotion(), root_page_id="R", store=store).can_upload


def test_metadata_blocks_are_searchable(config, store):
    fake = RecordingNotion()
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    texts = [
        rich["text"]["content"]
        for blocks in fake.blocks.values()
        for block in blocks
        if block.get("type") == "paragraph"
        for rich in block["paragraph"]["rich_text"]
    ]
    joined = "\n".join(texts)
    for expected in ("Date:", "Ticker:", "Company:", "AI Source:", "File Type:", "SHA256:", "Part:", "Total Parts:"):
        assert expected in joined
    assert "AI Source: Claude" in joined
    assert "Ticker: 09696.HK" in joined
    assert "Company: 天齐锂业" in joined


def test_split_parts_are_uploaded_separately_with_part_labels(config, store):
    fake = RecordingNotion()
    big = "".join(f"<section><p>{'z' * 200_000}</p></section>" for _ in range(40))
    (config.inbox_dir / "Gemini KAP成本曲线.html").write_text(
        f"<html><head><title>KAP</title></head><body>{big}</body></html>", encoding="utf-8"
    )
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok
    assert len(fake.uploads) >= 2
    assert all("_Part" in name for name in fake.uploads)
    record = store.list_records()[0]
    assert record.status == STATUS_SPLIT
    assert record.total_parts == len(fake.uploads)


def test_root_page_failure_is_reported(config, store):
    fake = RecordingNotion(fail_page_lookup=True)
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.failed == 1
    assert "NOTION_ROOT_PAGE_ID 无效" in store.list_records()[0].error


def test_build_archiver_uses_env_config(monkeypatch, config, tmp_path):
    """复用现有 load_notion_config（同一个 Token / Root Page，不新建第二套）。"""
    monkeypatch.setenv("NOTION_TOKEN", "secret-token")
    monkeypatch.setenv("NOTION_ROOT_PAGE_ID", "root-from-env")
    archiver = build_archiver(config, client=RecordingNotion(), store=None)
    assert archiver.root_page_id == "root-from-env"
    assert archiver.can_upload


def test_build_archiver_without_token_still_constructs(config, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_ROOT_PAGE_ID", raising=False)
    archiver = build_archiver(config, store=None)
    assert not archiver.can_upload  # 由 pipeline 明确报错，而不是崩溃
