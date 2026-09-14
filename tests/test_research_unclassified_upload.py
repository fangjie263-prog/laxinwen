from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from news.research.archive_store import ResearchArchiveStore
from news.research.config import ResearchArchiveConfig
from news.research.notion_archive import ResearchNotionArchiver
from news.research.pipeline import run_research_archive
from news.research.scanner import ResearchArchiveScanner
from news.research.splitter import split_xlsx


class FakeNotion:
    def __init__(self):
        self.pages = {}
        self.created = []
        self.uploads = []
        self.blocks = {}
        self.counter = 0

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
        self.created.append((parent_id, title))
        return page_id

    def upload_file(self, path, content_type=None):
        self.counter += 1
        self.uploads.append(Path(path).name)
        return f"upload{self.counter}"

    def append_blocks(self, page_id, blocks):
        self.blocks.setdefault(page_id, []).extend(blocks)


@pytest.fixture()
def config(tmp_path):
    value = ResearchArchiveConfig(
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        db_path=tmp_path / "research.sqlite",
        companies_file=None,
        dry_run=False,
    )
    value.ensure_dirs()
    return value


def _run(config, fake):
    with ResearchArchiveStore(config.db_path) as store:
        archiver = ResearchNotionArchiver(config, client=fake, root_page_id="ROOT", store=store)
        result = run_research_archive(config=config, store=store, archiver=archiver)
        record = store.list_records()[0] if store.list_records() else None
    return result, record


def _write_xlsx(path: Path, rows: int = 20):
    sheet = "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData>"
    sheet += "".join(
        f"<row r=\"{i}\"><c r=\"A{i}\" t=\"inlineStr\"><is><t>mystery-{i}-research-data</t></is></c></row>"
        for i in range(1, rows + 1)
    )
    sheet += "</sheetData></worksheet>"
    content_types = (
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Override PartName=\"/xl/worksheets/sheet1.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
        "</Types>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


@pytest.mark.parametrize("suffix", [".md", ".txt", ".xlsx"])
def test_unknown_research_material_uploads_under_date(config, suffix):
    path = config.inbox_dir / f"20260912 personal-research{suffix}"
    if suffix == ".xlsx":
        _write_xlsx(path)
    else:
        path.write_text("my research conversation", encoding="utf-8")
    fake = FakeNotion()
    result, record = _run(config, fake)

    assert result.uploaded == 1
    assert record and record.notion_page_id
    archived = list((config.archive_dir / "2026-09-12").glob(f"*{suffix}"))
    assert len(archived) == 1
    assert "Unknown" not in str(archived[0].parent)
    assert not any(title == "Unknown" for _, title in fake.created)
    assert any(parent != "ROOT" and title == archived[0].stem for parent, title in fake.created)


def test_unreadable_doc_and_identity_conflict_still_upload(config):
    unreadable = config.inbox_dir / "20260912 000660.KS SK海力士.doc"
    unreadable.write_bytes(b"not a readable legacy word document")
    fake = FakeNotion()
    candidate = ResearchArchiveScanner(config).scan()[0]
    assert candidate.content_status == "UNREADABLE"
    result, _ = _run(config, fake)
    assert result.uploaded == 1

    # Separate config/store for the conflict case.
    config2 = ResearchArchiveConfig(
        inbox_dir=config.inbox_dir.parent / "inbox2",
        archive_dir=config.archive_dir.parent / "archive2",
        failed_dir=config.failed_dir.parent / "failed2",
        db_path=config.db_path.parent / "conflict.sqlite",
        companies_file=None,
        dry_run=False,
    )
    config2.ensure_dirs()
    html = config2.inbox_dir / "20260912 09696.HK 天齐锂业.html"
    html.write_text("<html><body>重庆机电 研究正文</body></html>", encoding="utf-8")
    conflict = ResearchArchiveScanner(config2).scan()[0]
    assert conflict.identity_status == "CONFLICT"
    fake2 = FakeNotion()
    result2, _ = _run(config2, fake2)
    assert result2.uploaded == 1
    assert list((config2.archive_dir / "2026-09-12").glob("09696.HK_天齐锂业/*.html"))


def test_unknown_ticker_known_company_is_classified_and_uploaded(config):
    path = config.inbox_dir / "20260912 盐湖股份研究.txt"
    path.write_text("盐湖股份研究资料", encoding="utf-8")
    fake = FakeNotion()
    result, record = _run(config, fake)
    assert result.uploaded == 1
    assert record and record.ticker == "000792.SZ"
    assert list((config.archive_dir / "2026-09-12").glob("000792.SZ_盐湖股份/*.txt"))


def test_xlsx_split_parts_are_valid_workbooks(tmp_path):
    source = tmp_path / "source.xlsx"
    target = tmp_path / "parts"
    _write_xlsx(source, rows=200)
    result = split_xlsx(source, target, stem="report", max_bytes=2048)
    assert result.ok and len(result.parts) > 1
    for part in result.parts:
        assert part.stat().st_size <= 2048
        with zipfile.ZipFile(part) as archive:
            assert "[Content_Types].xml" in archive.namelist()
            assert "xl/worksheets/sheet1.xml" in archive.namelist()
