"""SQLite 状态 / 去重库测试（对应需求二十二、二十三、二十四、三十二）。"""

from __future__ import annotations

import pytest

from news.research.archive_store import (
    STATUS_ARCHIVED,
    STATUS_FAILED,
    STATUS_SPLIT,
    STATUS_UPLOADED,
    ResearchArchiveStore,
    ResearchFileRecord,
    sha256_bytes,
    sha256_file,
)


@pytest.fixture()
def store(tmp_path):
    with ResearchArchiveStore(tmp_path / "research.db") as instance:
        yield instance


def test_sha256_file_is_content_based(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "different_name.pdf"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    assert sha256_file(a) == sha256_file(b) == sha256_bytes(b"same content")


def test_sha256_differs_for_different_content(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert sha256_file(a) != sha256_file(b)


def test_upsert_is_idempotent_on_sha256(store):
    record = ResearchFileRecord(
        sha256="deadbeef", normalized_filename="20260912A_Claude_09696.HK_天齐锂业_研究.pdf",
        date="2026-09-12", ticker="09696.HK", company="天齐锂业", ai_source="Claude",
        status=STATUS_ARCHIVED,
    )
    store.upsert(record)
    store.upsert(record)
    assert len(store.list_records()) == 1


def test_find_by_sha256_and_archive_path(store):
    store.upsert(
        ResearchFileRecord(
            sha256="abc", archive_path="/archive/2026-09-12/A/x.pdf",
            normalized_filename="20260912A_Claude_A_x.pdf", date="2026-09-12",
        )
    )
    assert store.find_by_sha256("abc") is not None
    assert store.find_by_archive_path("/archive/2026-09-12/A/x.pdf") is not None
    assert store.find_by_sha256("missing") is None


def test_mark_failed_records_reason(store):
    store.upsert(ResearchFileRecord(sha256="f1"))
    store.mark_failed("f1", "切割失败：单页超限")
    record = store.find_by_sha256("f1")
    assert record.status == STATUS_FAILED
    assert "单页超限" in record.error


def test_uploaded_flag_requires_page_and_status(store):
    store.upsert(ResearchFileRecord(sha256="u1"))
    assert not store.find_by_sha256("u1").uploaded
    store.mark("u1", status=STATUS_UPLOADED, notion_page_id="page-1")
    record = store.find_by_sha256("u1")
    assert record.uploaded
    assert record.notion_page_id == "page-1"
    assert record.uploaded_at


def test_split_status_is_also_considered_uploaded(store):
    store.upsert(ResearchFileRecord(sha256="s1"))
    store.mark("s1", status=STATUS_SPLIT, notion_page_id="page-2", total_parts=3)
    record = store.find_by_sha256("s1")
    assert record.uploaded
    assert record.total_parts == 3


def test_used_letters_is_scoped_per_date_and_company(store):
    for letter, date, company in (
        ("A", "2026-09-12", "09696.HK_天齐锂业"),
        ("B", "2026-09-12", "09696.HK_天齐锂业"),
        ("A", "2026-09-12", "NVDA_NVIDIA"),
        ("A", "2026-09-13", "09696.HK_天齐锂业"),
    ):
        store.upsert(
            ResearchFileRecord(
                sha256=f"{date}-{company}-{letter}",
                normalized_filename=f"2026091{0 if date.endswith('12') else 3}{letter}_Claude_x_y_z.pdf",
                date=date,
                archive_path=f"/archive/{date}/{company}/f-{letter}.pdf",
            )
        )
    assert store.used_letters(date="2026-09-12", company_dir="09696.HK_天齐锂业") == {"A", "B"}
    assert store.used_letters(date="2026-09-12", company_dir="NVDA_NVIDIA") == {"A"}
    assert store.used_letters(date="2026-09-13", company_dir="09696.HK_天齐锂业") == {"A"}


def test_count_by_status_and_filters(store):
    store.upsert(ResearchFileRecord(sha256="a", status=STATUS_ARCHIVED, ticker="NVDA", ai_source="Claude", date="2026-09-12"))
    store.upsert(ResearchFileRecord(sha256="b", status=STATUS_UPLOADED, ticker="NVDA", ai_source="Gemini", date="2026-09-12"))
    store.upsert(ResearchFileRecord(sha256="c", status=STATUS_FAILED, ticker="KAP", ai_source="Unknown", date="2026-09-11"))
    counts = store.count_by_status()
    assert counts[STATUS_ARCHIVED] == 1
    assert counts[STATUS_UPLOADED] == 1
    assert counts[STATUS_FAILED] == 1
    assert len(store.list_records(ticker="NVDA")) == 2
    assert len(store.list_records(ai_source="Unknown")) == 1
    assert len(store.list_records(date="2026-09-11")) == 1
    # 支持按 Unknown 检索（需求三十）
    assert store.list_records(ai_source="Unknown")[0].ticker == "KAP"


def test_store_is_separate_from_news_db(tmp_path):
    """状态库独立于 data/news.db，不污染既有 news schema。"""
    from news.research.config import ResearchArchiveConfig

    config = ResearchArchiveConfig(db_path=tmp_path / "research" / "research_archive.db")
    config.ensure_dirs()
    with ResearchArchiveStore(config.db_path) as instance:
        instance.upsert(ResearchFileRecord(sha256="x"))
    assert config.db_path.name == "research_archive.db"
