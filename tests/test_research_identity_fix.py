from __future__ import annotations

from news.research.audit import format_audit, inspect_archive, repair_archive
from news.research.archive_store import ResearchArchiveStore, ResearchFileRecord
from news.research.company import CompanyDirectory
from news.research.config import ResearchArchiveConfig
from news.research.document_text import CONTENT_UNREADABLE, read_doc_text
from news.research.scanner import ResearchArchiveScanner


def test_company_names_and_ticker_variants_share_canonical_identity():
    directory = CompanyDirectory()
    names = [
        directory.detect("重庆机电 深度研究.pdf").directory_name,
        directory.detect("2722.HK 重庆机电 Claude.pdf").directory_name,
        directory.detect("02722.HK 重庆机电 Gemini.pdf").directory_name,
    ]
    assert names == ["02722.HK_重庆机电"] * 3
    assert directory.detect("盐湖股份 深度研究.pdf").directory_name == "000792.SZ_盐湖股份"


def test_unreadable_doc_does_not_expose_byte_fallback(monkeypatch, tmp_path):
    path = tmp_path / "SK海力士20260831.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0" + "ChatGPT NVDA NVIDIA".encode("utf-16-le"))
    monkeypatch.setattr("news.research.document_text._shutil.which", lambda _: None)
    document = read_doc_text(path)
    assert document.content_status == CONTENT_UNREADABLE
    assert document.snippets == ()


def test_filename_content_conflict_requires_review(tmp_path):
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    path = inbox / "重庆机电 深度研究.html"
    path.write_text("<html><body>天齐锂业 09696.HK</body></html>", encoding="utf-8")
    cfg = ResearchArchiveConfig(inbox_dir=inbox, archive_dir=archive, failed_dir=tmp_path / "failed", companies_file=None)
    candidate = ResearchArchiveScanner(cfg).scan()[0]
    assert candidate.identity_status == "CONFLICT"
    assert candidate.company_dir == "REVIEW_REQUIRED"
    assert "ACTION=REVIEW_REQUIRED" in candidate.plan_line(1)


def test_archive_audit_is_read_only_and_reports_split(tmp_path):
    date_dir = tmp_path / "2026-08-31"
    (date_dir / "重庆机电").mkdir(parents=True)
    (date_dir / "2722.HK_重庆机电").mkdir()
    marker = date_dir / "2722.HK_重庆机电" / "report.pdf"
    marker.write_bytes(b"keep")
    entries = inspect_archive(tmp_path)
    lines = format_audit(entries, tmp_path)
    assert any(item.identity_status == "CANONICALIZE_REQUIRED" for item in entries)
    assert any("historical_error=YES" in line for line in lines)
    assert any("02722.HK_重庆机电" in line for line in lines)
    assert marker.read_bytes() == b"keep"


def test_repair_dry_run_is_read_only_and_apply_preserves_store_identity(tmp_path):
    source_dir = tmp_path / "2026-09-12" / "2722.HK_重庆机电"
    source_dir.mkdir(parents=True)
    source = source_dir / "20260912A_ChatGPT_2722.HK_重庆机电_研究.pdf"
    source.write_bytes(b"%PDF-identity")
    db = tmp_path / "research.db"
    with ResearchArchiveStore(db) as store:
        store.upsert(ResearchFileRecord(
            sha256="identity-sha", archive_path=str(source), ticker="2722.HK", company="重庆机电",
            notion_page_id="page-existing", status="UPLOADED",
        ))
    entries = inspect_archive(tmp_path, store=None)
    dry_run = repair_archive(tmp_path, entries, apply=False)
    assert source.is_file()
    assert any("CANONICAL_TICKER" in line for line in dry_run)
    with ResearchArchiveStore(db) as store:
        repair_archive(tmp_path, entries, store=store, apply=True)
        moved = tmp_path / "2026-09-12" / "02722.HK_重庆机电" / source.name
        record = store.find_by_archive_path(moved)
        assert moved.is_file()
        assert record is not None
        assert record.notion_page_id == "page-existing"
