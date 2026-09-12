from __future__ import annotations

from news.research.audit import audit_archive
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
    lines = audit_archive(tmp_path)
    assert any("identity split" in line for line in lines)
    assert any("02722.HK_重庆机电" in line for line in lines)
    assert marker.read_bytes() == b"keep"
