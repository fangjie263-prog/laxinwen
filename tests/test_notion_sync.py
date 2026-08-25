import os
import json
import zipfile
from pathlib import Path

import pytest

from news.notion_sync import (
    ExportPackageScanner,
    NotionSync,
    NotionSyncError,
    ResearchReaderScanner,
    _build_html_artifacts,
    _build_word_artifacts,
    notion_max_upload_bytes,
)


class FakeNotion:
    def __init__(self, *, fail_after=None):
        self.created = []
        self.uploads = []
        self.blocks = []
        self.fail_after = fail_after

    def retrieve_page(self, page_id):
        return {"id": page_id}

    def find_or_create_child_page(self, parent_id, title):
        page_id = f"page-{len(self.created) + 1}"
        self.created.append((parent_id, title, page_id))
        return page_id

    def upload_file(self, path, *, content_type=None):
        if self.fail_after is not None and len(self.uploads) >= self.fail_after:
            raise RuntimeError("simulated upload failure")
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads.append((path.name, content_type, upload_id))
        return upload_id

    def append_blocks(self, page_id, blocks):
        self.blocks.append((page_id, blocks))


def _make_package(root: Path, name: str, article_count: int = 2, *, docx: bool = True, article_bytes: int = 0, docx_bytes: bytes | None = None):
    root.mkdir(parents=True, exist_ok=True)
    package = root / name
    package.mkdir()
    sections = "".join(f'<section id="article-{i}"></section>' for i in range(1, article_count + 1))
    (package / "index.html").write_text(sections, encoding="utf-8")
    (package / "articles").mkdir()
    for index in range(1, article_count + 1):
        article_path = package / "articles" / f"{index:03d}.html"
        if article_bytes:
            article_path.write_bytes(os.urandom(article_bytes))
        else:
            article_path.write_text("article", encoding="utf-8")
    (package / "server.py").write_text("# server", encoding="utf-8")
    if docx:
        (package / f"{name}.docx").write_bytes(docx_bytes if docx_bytes is not None else b"docx")
    return package


def test_scanner_identifies_source_date_job_and_files(tmp_path):
    root = tmp_path / "portable"
    package = _make_package(root, "Laxinwen-ECO-2026-08-24-142002-test")
    _make_package(root, "Laxinwen-RFI-2026-08-25-090000-morning", docx=False)

    found = ExportPackageScanner(root).scan()

    assert len(found) == 1
    item = found[0]
    assert item.source == "eco"
    assert item.date == "2026-08-24"
    assert item.job_id == "test"
    assert item.package_path == package
    assert item.index_path.name == "index.html"
    assert item.docx_path.suffix == ".docx"
    assert item.article_count == 2
    assert item.package_key


def test_sync_is_idempotent_and_groups_jobs_by_source_date(tmp_path):
    root = tmp_path / "portable"
    first = _make_package(root, "Laxinwen-ECO-2026-08-24-142002-morning")
    second = _make_package(root, "Laxinwen-ECO-2026-08-24-180000-evening")
    packages = ExportPackageScanner(root).scan()
    fake = FakeNotion()
    sync = NotionSync(fake, "root", state_path=tmp_path / "state.json")

    first_run = sync.sync(packages)
    second_run = sync.sync(packages)

    assert first_run == ["SYNC SUCCESS · ECO · 2026-08-24", "SYNC SUCCESS · ECO · 2026-08-24"]
    assert all("already synced" in message for message in second_run)
    assert [title for _, title, _ in fake.created] == [
        "ECO", "2026-08-24", "14:20:02 · morning", "18:00:00 · evening"
    ]
    assert len(fake.uploads) == 4
    assert len(fake.blocks) == 2
    assert first.exists() and second.exists()


def test_same_source_date_different_run_ids_create_distinct_run_pages(tmp_path):
    root = tmp_path / "portable"
    first = _make_package(root, "Laxinwen-RFI-2026-08-25-20260825-080012-rfi-default")
    second = _make_package(root, "Laxinwen-RFI-2026-08-25-20260825-141035-rfi-default")
    packages = ExportPackageScanner(root).scan()
    assert [item.run_id for item in packages] == ["20260825-080012", "20260825-141035"]
    fake = FakeNotion()
    sync = NotionSync(fake, "root", state_path=tmp_path / "state.json")

    sync.sync(packages)

    assert [title for _, title, _ in fake.created] == [
        "RFI", "2026-08-25", "08:00:12 · rfi-default", "14:10:35 · rfi-default"
    ]
    assert first.exists() and second.exists()
    assert len(fake.blocks) == 2
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    identities = [
        identity for record in state["packages"].values()
        for identity in record.get("artifacts", {})
    ]
    assert any(identity.startswith("laxinwen|rfi|2026-08-25|20260825-080012|") for identity in identities)


def test_researchreader_scanner_only_selects_daily_html_images_and_books(tmp_path):
    output = tmp_path / "output"
    package = output / "barrons_24-08-2026_Kobo"
    (package / "images").mkdir(parents=True)
    (package / "daily.html").write_text("<html/>", encoding="utf-8")
    (package / "images" / "cover.jpg").write_bytes(b"image")
    (package / "candidate_articles.json").write_text("debug", encoding="utf-8")
    books = tmp_path / "books"
    books.mkdir()
    book = books / "barrons 24-08-2026 (Kobo).epub"
    book.write_bytes(b"epub")

    found = ResearchReaderScanner(output, books).scan()

    assert len(found) == 1
    item = found[0]
    assert item.origin == "researchreader"
    assert item.source == "Barron's"
    assert item.date == "2026-08-24"
    assert item.run_id == ""
    assert {path.name for path in item.html_files} == {"daily.html", "cover.jpg"}
    assert item.extra_files == (book,)


def test_legacy_synced_state_is_skipped_without_reupload(tmp_path):
    root = tmp_path / "portable"
    _make_package(root, "Laxinwen-RFI-2026-08-24")
    package = ExportPackageScanner(root).scan()[0]
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "packages": {package.package_key: {"synced": True, "html_upload_id": "old-html", "word_upload_id": "old-word"}},
        "date_pages": {"rfi:2026-08-24": "old-date"},
        "source_pages": {"rfi": "old-source"},
    }), encoding="utf-8")
    fake = FakeNotion()

    messages = NotionSync(fake, "root", state_path=state_path).sync([package])

    assert "already synced" in messages[0]
    assert fake.uploads == []
    assert fake.created == []


def test_html_artifacts_exclude_docx_and_preserve_local_package(tmp_path):
    package_dir = _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24", article_count=2)
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]
    original_docx = package.docx_path.read_bytes()

    artifacts = _build_html_artifacts(package, tmp_path / "upload", 64 * 1024)

    assert all(item.path.suffix.lower() != ".docx" for item in artifacts)
    for item in artifacts:
        if item.kind == "html_zip":
            with zipfile.ZipFile(item.path) as archive:
                assert all(not name.lower().endswith(".docx") for name in archive.namelist())
    assert package_dir.joinpath(package.docx_path.name).read_bytes() == original_docx


def test_small_html_and_word_are_single_uploads(tmp_path):
    _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24")
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]

    html = _build_html_artifacts(package, tmp_path / "upload", 64 * 1024)
    word = _build_word_artifacts(package, tmp_path / "upload", 64 * 1024)

    assert len(html) == 1 and html[0].kind == "html_zip"
    assert len(word) == 1 and word[0].path == package.docx_path


def test_large_html_is_split_into_valid_bounded_zips(tmp_path):
    _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24", article_count=4, article_bytes=3 * 1024)
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]
    threshold = 8 * 1024

    artifacts = _build_html_artifacts(package, tmp_path / "upload", threshold)
    zips = [item for item in artifacts if item.kind == "html_zip"]

    assert len(zips) >= 2
    assert all(item.path.stat().st_size <= threshold for item in artifacts)
    names = []
    for item in zips:
        with zipfile.ZipFile(item.path) as archive:
            assert archive.testzip() is None
            assert all(not name.lower().endswith(".docx") for name in archive.namelist())
            names.extend(archive.namelist())
    assert len(names) == len(set(names))
    assert "index.html" in names and "server.py" in names


def test_oversized_single_html_file_uses_bounded_fallback(tmp_path):
    _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24", article_count=1, article_bytes=20 * 1024)
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]
    threshold = 4 * 1024

    artifacts = _build_html_artifacts(package, tmp_path / "upload", threshold)

    assert any(item.kind == "html_split" for item in artifacts)
    assert any(item.kind == "html_split_manifest" for item in artifacts)
    assert all(item.path.stat().st_size <= threshold for item in artifacts)


def test_oversized_word_uses_bounded_fallback(tmp_path):
    _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24", docx_bytes=os.urandom(20 * 1024))
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]
    threshold = 4 * 1024

    artifacts = _build_word_artifacts(package, tmp_path / "upload", threshold)

    assert all(item.path.stat().st_size <= threshold for item in artifacts)
    assert sum(item.kind == "word_split" for item in artifacts) > 1
    assert any(item.kind == "word_split_manifest" for item in artifacts)


def test_upload_state_resumes_only_missing_artifacts(tmp_path):
    _make_package(tmp_path / "portable", "Laxinwen-RFI-2026-08-24")
    package = ExportPackageScanner(tmp_path / "portable").scan()[0]
    state_path = tmp_path / "state.json"
    fake = FakeNotion(fail_after=1)
    sync = NotionSync(fake, "root", state_path=state_path, max_upload_bytes=64 * 1024)

    assert "SYNC FAILED" in sync.sync([package])[0]
    assert not sync.state["packages"][package.package_key].get("synced")
    assert len(fake.uploads) == 1

    fake.fail_after = None
    assert sync.sync([package])[0].startswith("SYNC SUCCESS")
    assert len(fake.uploads) == 2
    assert len(fake.blocks) == 1


def test_max_upload_env_and_default(monkeypatch):
    monkeypatch.delenv("NOTION_MAX_UPLOAD_MB", raising=False)
    assert notion_max_upload_bytes() == int(4.5 * 1024 * 1024)
    monkeypatch.setenv("NOTION_MAX_UPLOAD_MB", "1.25")
    assert notion_max_upload_bytes() == int(1.25 * 1024 * 1024)
    with pytest.raises(NotionSyncError):
        monkeypatch.setenv("NOTION_MAX_UPLOAD_MB", "0")
        notion_max_upload_bytes()
