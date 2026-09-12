"""归档主流程测试（对应需求二十二～二十四、三十三、三十四、四十五）。

覆盖：
- dry-run 绝对不移动 / 不重命名 / 不上传；
- SHA-256 相同 → 跳过归档与上传；
- 同一天同公司新增文件 → 序号递增（A / B）；
- 失败时原文件不丢失；
- 超过 4.5MB → 正确切割。
"""

from __future__ import annotations

import shutil

import pytest

from news.research.archive_store import (
    STATUS_DUPLICATE,
    STATUS_SPLIT,
    STATUS_UPLOADED,
    ResearchArchiveStore,
)
from news.research.config import ResearchArchiveConfig
from news.research.notion_archive import ResearchNotionArchiver
from news.research.pipeline import run_research_archive
from news.research.scanner import ResearchArchiveScanner

from research_helpers import make_docx, make_html, make_pdf


class FakeNotion:
    """最小 Notion 客户端替身（覆盖首次上传 / 重复上传 / 上传失败 / 恢复）。"""

    def __init__(self, *, fail_uploads: int = 0):
        self.pages: dict[str, dict[str, dict]] = {}
        self.blocks: dict[str, list] = {}
        self.uploaded_files: list[str] = []
        self.counter = 0
        self.fail_uploads = fail_uploads

    # --- NotionClient 接口 ---
    def retrieve_page(self, page_id):
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
        if self.fail_uploads > 0:
            self.fail_uploads -= 1
            raise RuntimeError("simulated upload failure")
        self.counter += 1
        self.uploaded_files.append(path.name)
        return f"upload{self.counter}"

    def append_blocks(self, page_id, blocks):
        self.blocks.setdefault(page_id, []).extend(blocks)

    def close(self):
        pass

    # --- 断言辅助 ---
    @property
    def page_titles(self) -> set[str]:
        titles: set[str] = set()
        for children in self.pages.values():
            titles.update(children.keys())
        return titles

    @property
    def uploaded_block_files(self) -> int:
        return sum(
            1 for blocks in self.blocks.values()
            for block in blocks if block.get("type") == "file"
        )


@pytest.fixture()
def config(tmp_path):
    cfg = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "research.db",
        companies_file=None,
        dry_run=True,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def store(config):
    with ResearchArchiveStore(config.db_path) as instance:
        yield instance


def make_archiver(config, store, fake):
    return ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store)


# ---------- dry-run ----------

def test_dry_run_never_moves_or_uploads(config, store):
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.dry_run
    assert result.scanned == 1
    assert result.archived == 0
    assert result.uploaded == 0
    assert (config.inbox_dir / "Claude 天齐锂业深度研究.html").is_file()  # 原文件未动
    assert list(config.archive_dir.rglob("*")) == []                    # 未归档
    assert fake.uploaded_files == []                                     # 未上传
    assert store.list_records() == []                                    # 未写库
    assert any("DRY-RUN" in message for message in result.messages)


# ---------- apply ----------

def test_apply_archives_moves_and_uploads(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.ok
    assert result.archived == 1
    assert result.uploaded == 1
    assert not (config.inbox_dir / "Claude 天齐锂业深度研究.html").exists()  # 归档后才清理
    archived = list(config.archive_dir.rglob("*.html"))
    assert len(archived) == 1
    assert archived[0].name.startswith("2026") or "_Claude_09696.HK_天齐锂业_" in archived[0].name
    assert "09696.HK_天齐锂业" in str(archived[0])
    assert fake.uploaded_block_files == 1
    record = store.list_records()[0]
    assert record.status == STATUS_UPLOADED
    assert record.notion_page_id


def test_second_identical_file_is_skipped_entirely(config, store):
    """需求二十四、四十五：SHA256 相同 → 不重复归档、不重复上传。"""
    config.dry_run = False
    content = "<html><head><title>Claude 天齐锂业</title></head><body>研究正文</body></html>"
    (config.inbox_dir / "Claude 天齐锂业深度研究.html").write_text(content, encoding="utf-8")
    fake = FakeNotion()
    archiver = make_archiver(config, store, fake)

    first = run_research_archive(config=config, store=store, archiver=archiver)
    assert first.uploaded == 1
    uploads_after_first = list(fake.uploaded_files)

    # 同内容、不同文件名，再次放进 Inbox
    (config.inbox_dir / "Claude 天齐锂业深度研究最终版.html").write_text(content, encoding="utf-8")
    second = run_research_archive(config=config, store=store, archiver=archiver)
    assert second.duplicates == 1
    assert second.uploaded == 0
    assert fake.uploaded_files == uploads_after_first      # 没有第二次上传
    assert len(list(config.archive_dir.rglob("*.html"))) == 1


def test_same_day_same_company_increments_letter(config, store):
    """需求四十五 - 6：同一天同公司新增文件 → B。"""
    config.dry_run = False
    # 文件名排序决定分配顺序（扫描顺序稳定）：Claude 先 → A，ChatGPT 后 → B
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html", sections=2)
    make_docx(config.inbox_dir / "ChatGPT 天齐锂业锂价分析.docx", chapters=2)
    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.ok
    names = sorted(path.name for path in (config.archive_dir).rglob("*") if path.is_file())
    assert len(names) == 2
    letters = sorted(name[:9][-1] for name in names)
    assert letters == ["A", "B"], names
    by_letter = {name[:9][-1]: name for name in names}
    # 每个公司目录独立编号：同一天同公司 2 个文件 → A、B（与 AI 顺序无关）
    assert set(by_letter) == {"A", "B"}
    assert "_Claude_09696.HK_天齐锂业_" in " ".join(by_letter.values())
    assert "_ChatGPT_09696.HK_天齐锂业_" in " ".join(by_letter.values())


def test_unknown_ai_and_company_land_in_unknown_dir(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "行业研究.html", body="普通研究正文", title="行业研究")
    fake = FakeNotion()
    run_research_archive(config=config, store=store, archiver=make_archiver(config, store, fake))
    # 需求五：公司未识别时不生成 Unknown_公司 / Unknown_Unknown 占位目录
    assert list(config.archive_dir.rglob("Unknown"))
    assert not list(config.archive_dir.rglob("Unknown_Unknown"))
    assert not list(config.archive_dir.rglob("Unknown_*"))
    record = store.list_records()[0]
    assert record.ai_source == "Unknown"
    assert record.notion_page_id  # 仍然上传，AI Source = Unknown 可检索


def test_oversized_file_is_split_and_parts_uploaded(config, store):
    config.dry_run = False
    big = "".join(f"<section><p>{'x' * 200_000}</p></section>" for _ in range(40))
    (config.inbox_dir / "Gemini KAP成本曲线.html").write_text(
        f"<html><head><title>KAP</title></head><body>{big}</body></html>", encoding="utf-8"
    )
    source = config.inbox_dir / "Gemini KAP成本曲线.html"
    assert source.stat().st_size > config.max_upload_bytes

    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.ok
    assert result.split == 1
    parts = sorted(config.archive_dir.rglob("*_Part*.html"))
    assert len(parts) >= 2
    for path in parts:
        assert path.stat().st_size <= config.max_upload_bytes
        assert path.read_text(encoding="utf-8").strip().startswith("<!DOCTYPE html>")
    record = store.list_records()[0]
    assert record.status == STATUS_SPLIT
    assert record.total_parts == len(parts)
    assert len(fake.uploaded_files) == len(parts)


# ---------- 失败与恢复 ----------

def test_upload_failure_keeps_file_and_marks_failed(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    fake = FakeNotion(fail_uploads=1)
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.failed == 1
    record = store.list_records()[0]
    assert record.status == "FAILED"
    assert "simulated upload failure" in record.error
    # 原文件不丢失（被移动到 Failed 而不是删除）
    moved = list(config.failed_dir.rglob("*.html"))
    assert len(moved) == 1
    assert moved[0].read_text(encoding="utf-8")


def test_corrupted_file_is_not_deleted_and_goes_to_failed(config, store):
    """需求三十四：无法读取 / 损坏文件不删除，移入 Failed 并记录原因。"""
    config.dry_run = False
    broken = config.inbox_dir / "ChatGPT 天齐锂业研究.docx"
    broken.write_bytes(b"not a real docx at all")
    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.failed == 1
    assert not broken.exists()
    failed = list(config.failed_dir.rglob("*.docx"))
    assert len(failed) == 1
    assert failed[0].read_bytes() == b"not a real docx at all"   # 内容未损坏
    assert store.list_records()[0].error


def test_apply_without_notion_credentials_fails_closed(config, store):
    """没有凭据时不静默归档：不移动任何文件，明确报错。"""
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    archiver = ResearchNotionArchiver(config, client=None, root_page_id="", store=store)
    result = run_research_archive(config=config, store=store, archiver=archiver)
    assert not result.ok
    assert (config.inbox_dir / "Claude 天齐锂业研究.html").is_file()
    assert list(config.archive_dir.rglob("*")) == []
    assert any("缺少 Notion 凭据" in message for message in result.messages)


def test_failed_upload_is_not_retried_without_flag(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    fake = FakeNotion(fail_uploads=1)
    archiver = make_archiver(config, store, fake)
    run_research_archive(config=config, store=store, archiver=archiver)
    assert fake.uploaded_files == []

    # 重新放入同内容文件（sha256 相同）→ 默认不重试
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    second = run_research_archive(config=config, store=store, archiver=archiver)
    assert second.duplicates == 1
    assert fake.uploaded_files == []


def test_retry_failed_flag_resumes_upload(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    fake = FakeNotion(fail_uploads=1)
    archiver = make_archiver(config, store, fake)
    run_research_archive(config=config, store=store, archiver=archiver)
    assert store.list_records()[0].status == "FAILED"

    # 把失败文件从 Failed 移回 Inbox，并显式允许重试
    failed_file = next(config.failed_dir.rglob("*.html"))
    shutil.move(str(failed_file), str(config.inbox_dir / failed_file.name))
    third = run_research_archive(
        config=config, store=store, archiver=archiver, retry_failed=True
    )
    assert third.uploaded == 1
    assert fake.uploaded_files
    assert store.list_records()[0].status == STATUS_UPLOADED


def test_failed_upload_never_marks_uploaded(config, store):
    """失败记录不允许被当作「已上传」，否则会永远跳过（需求三十二/二十四）。"""
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    fake = FakeNotion(fail_uploads=1)
    run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    record = store.list_records()[0]
    assert record.status == "FAILED"
    assert not record.uploaded
    assert not record.notion_page_id


def test_keep_inbox_option_leaves_original(config, store):
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    fake = FakeNotion()
    run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake),
        move_on_success=False,
    )
    assert (config.inbox_dir / "Claude 天齐锂业研究.html").is_file()
    assert len(list(config.archive_dir.rglob("*.html"))) == 1


def test_duplicate_file_is_recorded_as_duplicate(config, store):
    """重复文件的原文件被清理、不产生新归档、不产生第二次上传。"""
    config.dry_run = False
    content = "<html><head><title>Claude 天齐锂业</title></head><body>研究正文</body></html>"
    (config.inbox_dir / "Claude 天齐锂业研究.html").write_text(content, encoding="utf-8")
    fake = FakeNotion()
    archiver = make_archiver(config, store, fake)
    run_research_archive(config=config, store=store, archiver=archiver)

    (config.inbox_dir / "Claude 天齐锂业研究副本.html").write_text(content, encoding="utf-8")
    second = run_research_archive(config=config, store=store, archiver=archiver)
    assert second.duplicates == 1
    assert len(list(config.archive_dir.rglob("*.html"))) == 1
    assert store.list_records()[0].status == STATUS_UPLOADED
    assert len(fake.uploaded_files) == 1


def test_empty_inbox_is_success(config, store):
    config.dry_run = False
    fake = FakeNotion()
    result = run_research_archive(
        config=config, store=store, archiver=make_archiver(config, store, fake)
    )
    assert result.ok
    assert result.scanned == 0


def test_result_dict_shape(config, store):
    make_html(config.inbox_dir / "Claude 天齐锂业研究.html")
    result = run_research_archive(config=config, store=store)
    data = result.as_dict()
    for key in (
        "scanned", "new", "duplicates", "archived", "split",
        "uploaded", "upload_skipped", "failed", "moved", "dry_run",
    ):
        assert key in data
