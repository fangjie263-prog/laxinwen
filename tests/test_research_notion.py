"""Notion 集成测试（对应需求二十四、二十八、二十九、三十七）。

全部使用 mock 客户端，覆盖：首次上传 / 重复上传 / 上传失败 / 恢复，以及
研究报告专属页面结构（``Root → 研究报告 → 日期 → 公司 → 文件``）与极简页面正文。
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
from news.research.notion_archive import (
    RESEARCH_CATEGORY_TITLE,
    ResearchNotionArchiver,
    _has_forbidden_labels,
    build_archiver,
    report_page_body,
    report_page_title,
)
from news.research.pipeline import run_research_archive
from news.research.scanner import ResearchArchiveScanner

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
        self.created_pages: list[tuple[str, str]] = []  # (parent_id, title)
        self.date_position_calls: list[tuple[str, str]] = []  # (parent_id, date)

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
        self.created_pages.append((parent_id, title))
        return page_id

    # --- 断言辅助 ---
    def page_id(self, parent_id, title):
        return self.pages[parent_id][title]["id"]

    def titles_under(self, parent_id):
        return set(self.pages.get(parent_id, {}).keys())

    def paragraphs(self, page_id):
        return [
            rich["text"]["content"]
            for block in self.blocks.get(page_id, [])
            if block.get("type") == "paragraph"
            for rich in block["paragraph"]["rich_text"]
        ]

    @property
    def all_titles(self):
        return {title for children in self.pages.values() for title in children}

    def date_page_position(self, parent_id, date):
        self.date_position_calls.append((parent_id, date))
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


def test_first_upload_creates_category_date_company_and_report_pages(config, store):
    """需求一、三、四、六：Root → 研究报告 → 日期 → 公司 → 文件。"""
    fake = RecordingNotion()
    candidate = make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok
    assert fake.retrieve_page_calls == 1

    dates = store.list_records()[0].date
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, dates)
    company_id = fake.page_id(date_id, "09696.HK｜天齐锂业")
    assert fake.titles_under("ROOT") == {RESEARCH_CATEGORY_TITLE}
    assert fake.titles_under(company_id) == {"Claude｜天齐锂业"}
    # 研究文件页正文极简：日期 · Ticker + 📎 研究报告 + 附件
    report_id = fake.page_id(company_id, "Claude｜天齐锂业")
    paragraphs = fake.paragraphs(report_id)
    assert paragraphs[0] == f"{dates}｜09696.HK"
    assert paragraphs[1] == "📎 研究报告"


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


def test_report_page_body_is_minimal_and_hides_metadata(config, store):
    """需求五、六、十一：正文只显示日期 / Ticker / 研究报告附件，不暴露技术元数据。"""
    fake = RecordingNotion()
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    dates = store.list_records()[0].date
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    company_id = fake.page_id(fake.page_id(category_id, dates), "09696.HK｜天齐锂业")
    report_id = fake.page_id(company_id, "Claude｜天齐锂业")

    texts = fake.paragraphs(report_id)
    joined = "\n".join(texts)
    for forbidden in (
        "SHA256", "File Size", "Part", "Total Parts",
        "Original Filename", "Normalized Filename", "命中方式",
    ):
        assert forbidden not in joined
    # 必须包含：日期、Ticker、研究报告附件
    assert dates in joined
    assert "09696.HK" in joined
    assert "📎 研究报告" in joined
    # 附件作为 file block 存在于该文件页面
    file_blocks = [b for b in fake.blocks[report_id] if b.get("type") == "file"]
    assert len(file_blocks) == 1
    # 复用实现里的自检函数，确保正文不含任何被禁字段
    assert _has_forbidden_labels(fake.blocks[report_id]) is False


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


# ==========================================================================
# 研究报告页面结构（需求一 ~ 十二）
# ==========================================================================

def _upload_one(config, store, fake, filename="Claude 天齐锂业深度研究.html"):
    candidate = make_html(config.inbox_dir / filename)
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok
    return candidate


def test_category_page_created_when_missing(config, store):
    """需求十一 - 1：root 下不存在「研究报告」→ 自动创建。"""
    fake = RecordingNotion()
    _upload_one(config, store, fake)
    assert RESEARCH_CATEGORY_TITLE in fake.pages["ROOT"]
    assert ("ROOT", RESEARCH_CATEGORY_TITLE) in fake.created_pages


def test_existing_category_page_is_reused(config, store):
    """需求十一 - 2：root 下已存在「研究报告」→ 复用，不重复创建。"""
    fake = RecordingNotion()
    fake.find_or_create_child_page("ROOT", RESEARCH_CATEGORY_TITLE)  # 预先存在
    fake.created_pages.clear()
    _upload_one(config, store, fake)

    created_categories = [
        title for parent, title in fake.created_pages if title == RESEARCH_CATEGORY_TITLE
    ]
    assert created_categories == []                       # 未新建
    assert list(fake.pages["ROOT"].keys()).count(RESEARCH_CATEGORY_TITLE) == 1
    assert fake.created_pages == [
        ("page1", store.list_records()[0].date)            # 只新建了日期页
    ] or all(title != RESEARCH_CATEGORY_TITLE for _, title in fake.created_pages)


def test_single_date_page_per_date(config, store):
    """需求十一 - 3：同一个日期只创建一个日期页面。"""
    fake = RecordingNotion()
    _upload_one(config, store, fake, "Claude 天齐锂业深度研究.html")
    _upload_one(config, store, fake, "ChatGPT 天齐锂业锂价分析.html")  # 同一天同公司

    dates = {record.date for record in store.list_records()}
    assert len(dates) == 1
    date = dates.pop()
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_titles = [title for parent, title in fake.created_pages if parent == category_id]
    assert date_titles == [date]                     # 日期页只创建一次


def test_single_company_page_per_company(config, store):
    """需求十一 - 4：同一个公司只创建一个公司页面。"""
    fake = RecordingNotion()
    _upload_one(config, store, fake, "Claude 天齐锂业深度研究.html")
    _upload_one(config, store, fake, "ChatGPT 天齐锂业锂价分析.html")

    dates = {record.date for record in store.list_records()}
    date = dates.pop()
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, date)
    company_creations = [title for parent, title in fake.created_pages if parent == date_id]
    assert company_creations == ["09696.HK｜天齐锂业"]
    assert len(fake.pages[date_id]) == 1


def test_date_page_is_created_under_category_not_root(config, store):
    """需求十一 - 5、6：日期必须创建在「研究报告」下，不能直接挂在 root 下。"""
    fake = RecordingNotion()
    _upload_one(config, store, fake)
    date = store.list_records()[0].date

    assert "ROOT" in fake.pages
    assert date not in fake.pages["ROOT"]            # 日期不在 root 下
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    assert date in fake.pages[category_id]           # 日期在研究报告下
    # 日期页排序计算以「研究报告」为父页面进行
    assert fake.date_position_calls == [(category_id, date)]
    assert RESEARCH_CATEGORY_TITLE not in {title for title in fake.pages["ROOT"] if title != RESEARCH_CATEGORY_TITLE}


def test_report_page_title_and_body_contract(config, store):
    """需求六、八：标题 ``AI｜Company``，正文为日期 · Ticker + 📎 研究报告。"""
    fake = RecordingNotion()
    _upload_one(config, store, fake)
    record = store.list_records()[0]
    assert report_page_title(record) == "Claude｜天齐锂业"

    title = report_page_title(record)
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, record.date)
    company_id = fake.page_id(date_id, "09696.HK｜天齐锂业")
    report_id = fake.page_id(company_id, title)

    body = report_page_body(record)
    lines = [b["paragraph"]["rich_text"][0]["text"]["content"] for b in body]
    assert lines == [f"{record.date}｜09696.HK", "📎 研究报告"]
    assert fake.blocks[report_id][-1]["type"] == "file"


def test_multiple_gemini_reports_get_distinct_titles(config, store):
    """需求十一 - 9：同一天同一 AI 的多份文件，页面标题不冲突。"""
    fake = RecordingNotion()
    config.dry_run = False
    # 同一天、同一公司、同一 AI 的两份不同内容
    (config.inbox_dir / "Gemini 天齐锂业 锂价分析.html").write_text(
        "<html><head><title>Gemini</title></head><body>内容一</body></html>", encoding="utf-8"
    )
    (config.inbox_dir / "Gemini 天齐锂业 成本曲线.html").write_text(
        "<html><head><title>Gemini</title></head><body>内容二</body></html>", encoding="utf-8"
    )
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok and result.uploaded == 2

    record = store.list_records()[0]
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, record.date)
    company_id = fake.page_id(date_id, "09696.HK｜天齐锂业")
    titles = set(fake.pages[company_id].keys())
    assert len(titles) == 2, titles
    assert all(title.startswith("Gemini｜天齐锂业") for title in titles)
    # 两个页面各自只有自己的附件，不堆积
    for title in titles:
        page_id = fake.page_id(company_id, title)
        files = [b for b in fake.blocks[page_id] if b.get("type") == "file"]
        assert len(files) == 1


def test_each_file_gets_its_own_page(config, store):
    """需求七：每个文件对应一个独立页面，元数据不堆积在同一页面。"""
    fake = RecordingNotion()
    config.dry_run = False
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html", body="Claude 内容")
    make_html(config.inbox_dir / "ChatGPT 天齐锂业锂价分析.html", body="ChatGPT 内容")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok and result.uploaded == 2

    record = store.list_records()[0]
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, record.date)
    company_id = fake.page_id(date_id, "09696.HK｜天齐锂业")
    assert set(fake.pages[company_id]) == {"Claude｜天齐锂业", "ChatGPT｜天齐锂业"}
    for title in fake.pages[company_id]:
        page_id = fake.page_id(company_id, title)
        assert len([b for b in fake.blocks[page_id] if b.get("type") == "file"]) == 1


def test_existing_pages_are_not_deleted_or_migrated(config, store):
    """需求九：不自动删除 / 迁移旧页面；新逻辑只写新结构。"""
    fake = RecordingNotion()
    # 模拟旧结构：root 直接挂日期页
    legacy_date = fake.find_or_create_child_page("ROOT", "2026-09-04")
    legacy_company = fake.find_or_create_child_page(legacy_date, "09696.HK_天齐锂业")
    legacy_blocks = fake.blocks.setdefault(legacy_company, [])
    legacy_blocks.extend([{"object": "block", "type": "paragraph"}])

    _upload_one(config, store, fake)

    # 旧页面仍在，且没有被追加 / 修改
    assert fake.pages["ROOT"][ "2026-09-04"]["id"] == legacy_date
    assert "09696.HK_天齐锂业" in fake.pages[legacy_date]
    assert fake.blocks[legacy_company] == legacy_blocks
    # 新结构只创建研究文件页，未触碰旧日期页
    date = store.list_records()[0].date
    assert date != "2026-09-04"


def test_split_parts_share_one_report_page_with_part_labels(config, store):
    """分片上传仍指向同一个文件页面，标签清晰但不暴露技术元数据。"""
    fake = RecordingNotion()
    config.dry_run = False
    big = "".join(f"<section><p>{'z' * 200_000}</p></section>" for _ in range(40))
    (config.inbox_dir / "Gemini 天齐锂业 KAP成本曲线.html").write_text(
        f"<html><head><title>KAP</title></head><body>{big}</body></html>", encoding="utf-8"
    )
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    assert result.ok
    record = store.list_records()[0]
    assert record.status == STATUS_SPLIT
    category_id = fake.page_id("ROOT", RESEARCH_CATEGORY_TITLE)
    date_id = fake.page_id(category_id, record.date)
    company_id = fake.page_id(date_id, f"{record.ticker}｜{record.company}")
    assert set(fake.pages[company_id]) == {f"Gemini｜{record.company}"}
    page_id = fake.page_id(company_id, f"Gemini｜{record.company}")
    file_blocks = [b for b in fake.blocks[page_id] if b.get("type") == "file"]
    assert len(file_blocks) == record.total_parts >= 2
    joined = "\\n".join(fake.paragraphs(page_id))
    for forbidden in ("SHA256", "File Size", "Original Filename", "命中方式"):
        assert forbidden not in joined


def test_dry_run_message_uses_new_structure(config, store):
    """dry-run 提示也反映新结构（研究报告一级目录）。"""
    config.dry_run = True
    fake = RecordingNotion()
    make_html(config.inbox_dir / "Claude 天齐锂业深度研究.html")
    result = run_research_archive(
        config=config, store=store,
        archiver=ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store),
    )
    archiver = ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store)
    candidate = ResearchArchiveScanner(store=store, config=config).scan()[0]
    outcome = archiver.upload_candidate(candidate, dry_run=True)
    assert RESEARCH_CATEGORY_TITLE in outcome.message
    assert "PRIVATE" not in outcome.message
