from __future__ import annotations

from news.research.audit import format_audit, inspect_archive, manual_repair_entry, repair_archive
from news.research.archive_store import ResearchArchiveStore, ResearchFileRecord
from news.research.company import CompanyDirectory
from news.research.config import ResearchArchiveConfig
from news.research.document_text import CONTENT_UNREADABLE, read_doc_text
from news.research.scanner import ResearchArchiveScanner


def _historical(root, dirname: str, filename: str, payload: bytes = b"legacy"):
    path = root / "2026-09-12" / dirname / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


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
    entry = next(item for item in entries if item.path == source)
    assert entry.suggested_dir == "02722.HK_重庆机电"
    assert entry.suggested_filename == "20260912A_ChatGPT_02722.HK_重庆机电_研究.pdf"
    assert entry.target.name == entry.suggested_filename
    with ResearchArchiveStore(db) as store:
        repair_archive(tmp_path, entries, store=store, apply=True)
        moved = tmp_path / "2026-09-12" / "02722.HK_重庆机电" / entry.suggested_filename
        record = store.find_by_archive_path(moved)
        assert moved.is_file()
        assert record is not None
        assert record.notion_page_id == "page-existing"


def test_unknown_directory_and_filename_are_rebuilt_from_canonical_identity(tmp_path):
    cases = [
        ("Unknown", "20260912A_Claude_重庆机电_研究.pdf", "02722.HK_重庆机电"),
        ("Unknown_Unknown", "20260912A_Claude_Unknown_Unknown_盐湖股份_研究.pdf", "000792.SZ_盐湖股份"),
        ("Unknown_盐盐湖股份", "20260912A_Unknown_Unknown_盐盐湖股份_研究.pdf", "000792.SZ_盐湖股份"),
    ]
    for dirname, filename, suggested_dir in cases:
        root = tmp_path / dirname.replace("/", "_")
        source = _historical(root, dirname, filename)
        entry = inspect_archive(root)[0]
        assert entry.suggested_dir == suggested_dir
        assert suggested_dir.split("_", 1)[0] in entry.suggested_filename
        assert "Unknown_Unknown" not in entry.suggested_filename
        assert "盐盐湖股份" not in entry.suggested_filename
        assert source.is_file()


def test_review_required_never_moves_or_renames(tmp_path):
    root = tmp_path / "review"
    source = _historical(
        root,
        "NVDA_NVIDIA",
        "20260912A_Unknown_NVDA_NVIDIA_研究.html",
        "<html><body>重庆机电 02722.HK</body></html>".encode(),
    )
    entry = next(item for item in inspect_archive(root) if item.path == source)
    assert entry.identity_status == "REVIEW_REQUIRED"
    output = repair_archive(root, [entry], apply=True)
    assert any("SKIPPED" in line and "REVIEW_REQUIRED" in line for line in output)
    assert source.is_file()
    assert not (root / "2026-09-12" / "REVIEW_REQUIRED").exists()


def test_target_filename_conflict_is_skipped(tmp_path):
    root = tmp_path / "collision"
    source = _historical(root, "2722.HK_重庆机电", "20260912A_ChatGPT_2722.HK_重庆机电_研究.pdf")
    target = root / "2026-09-12" / "02722.HK_重庆机电" / "20260912A_ChatGPT_02722.HK_重庆机电_研究.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    entry = next(item for item in inspect_archive(root) if item.path == source)
    output = repair_archive(root, [entry], apply=True)
    assert any("TARGET_EXISTS" in line for line in output)
    assert source.is_file()
    assert target.read_bytes() == b"existing"


def test_manual_confirmed_review_file_repairs_filename_directory_and_sqlite(tmp_path):
    root = tmp_path / "archive"
    source = _historical(
        root,
        "NVDA_NVIDIA",
        "20260912A_Unknown_NVDA_NVIDIA_重庆机电深度价值与资本周期研判.pdf",
    )
    db = tmp_path / "research.db"
    with ResearchArchiveStore(db) as store:
        store.upsert(ResearchFileRecord(
            sha256="manual-sha", archive_path=str(source), ticker="NVDA", company="NVIDIA",
            notion_page_id="existing-page", status="UPLOADED",
        ))

    entry = manual_repair_entry(
        source, ticker="02722.HK", company="重庆机电", companies=CompanyDirectory()
    )
    assert entry.identity_status == "MANUAL_CONFIRMED"
    assert entry.suggested_dir == "02722.HK_重庆机电"
    assert entry.suggested_filename == "20260912A_Unknown_02722.HK_重庆机电_深度价值与资本周期研判.pdf"

    dry_run = repair_archive(root, [entry], apply=False)
    assert source.is_file()
    assert any(entry.suggested_filename in line for line in dry_run)

    with ResearchArchiveStore(db) as store:
        applied = repair_archive(root, [entry], store=store, apply=True)
        target = root / "2026-09-12" / entry.suggested_dir / entry.suggested_filename
        record = store.find_by_archive_path(target)
        assert any("APPLIED" in line for line in applied)
        assert target.is_file()
        assert not source.exists()
        assert record is not None
        assert record.notion_page_id == "existing-page"
