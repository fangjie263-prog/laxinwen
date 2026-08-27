import os
import json
import zipfile
from types import SimpleNamespace
from pathlib import Path

import pytest

from news.notion_sync import (
    ExportPackageScanner,
    NotionSync,
    NotionSyncError,
    ResearchReaderScanner,
    _build_html_artifacts,
    _build_extra_artifacts,
    _build_word_artifacts,
    _artifact_identity,
    _write_json,
    notion_max_upload_bytes,
    NotionClient,
)
from news.cli import cmd_notion_sync


class FakeNotion:
    def __init__(self, *, fail_after=None):
        self.created = []
        self.uploads = []
        self.blocks = []
        self.fail_after = fail_after

    def retrieve_page(self, page_id):
        return {"id": page_id}

    def find_or_create_child_page(self, parent_id, title, *, position=None):
        page_id = f"page-{len(self.created) + 1}"
        self.created.append((parent_id, title, page_id, position))
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
    assert [title for _, title, _, _ in fake.created] == [
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

    assert [title for _, title, _, _ in fake.created] == [
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


def test_researchreader_scanner_detects_translation_and_dual_html_variants(tmp_path):
    output = tmp_path / "output"
    package = output / "south-china-morning-post-6150_25-08-2026_Kobo"
    (package / "images").mkdir(parents=True)
    (package / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    translation = package / "daily-zh-CN-translation (7).html"
    dual = package / "daily-zh-CN-dual (15).html"
    translation.write_text("<html><body>中文</body></html>", encoding="utf-8")
    dual.write_text("<html><body>中英</body></html>", encoding="utf-8")

    found = ResearchReaderScanner(output, tmp_path / "books").scan()
    item = found[0]
    assert {path.name for path in item.extra_files} == {translation.name, dual.name}

    artifacts = _build_extra_artifacts(item, tmp_path / "upload", 64 * 1024)
    assert {artifact.artifact_variant for artifact in artifacts} == {
        "original", "zh-CN-translation", "zh-CN-dual"
    }
    assert all(artifact.kind == "html" for artifact in artifacts)
    assert all(artifact.path.suffix == ".html" for artifact in artifacts)
    assert all("images" not in artifact.path.parts for artifact in artifacts)


def test_large_translation_html_is_split_at_article_boundaries_without_zip(tmp_path):
    output = tmp_path / "output"
    package_dir = output / "barrons_25-08-2026_Kobo"
    package_dir.mkdir(parents=True)
    (package_dir / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    translated = package_dir / "daily-zh-CN-translation (7).html"
    translated.write_text(
        "<html><head><title>T</title></head><body>"
        + "".join(f"<article><h1>{i}</h1><p>{'x' * 250}</p></article>" for i in range(3))
        + "</body></html>", encoding="utf-8"
    )
    package = ResearchReaderScanner(output, tmp_path / "books").scan()[0]

    artifacts = _build_extra_artifacts(package, tmp_path / "upload", 600)
    assert len(artifacts) > 1
    assert sum(artifact.kind == "html_split" for artifact in artifacts) == 3
    assert any(artifact.artifact_variant == "original" for artifact in artifacts)
    assert all(artifact.path.suffix == ".html" for artifact in artifacts)
    assert all(artifact.path.stat().st_size <= 600 for artifact in artifacts)
    assert all(b"PK" not in artifact.path.read_bytes()[:2] for artifact in artifacts)
    assert all("<html" in artifact.path.read_text(encoding="utf-8") for artifact in artifacts)


def test_researchreader_original_html_has_distinct_zip_and_single_artifacts(tmp_path):
    output = tmp_path / "output"
    package_dir = output / "barrons_24-08-2026_Kobo"
    (package_dir / "images").mkdir(parents=True)
    (package_dir / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    (package_dir / "images" / "cover.jpg").write_bytes(b"image")
    package = ResearchReaderScanner(output, tmp_path / "books").scan()[0]

    html = _build_html_artifacts(package, tmp_path / "upload", 64 * 1024)
    extra = _build_extra_artifacts(package, tmp_path / "upload", 64 * 1024)
    original_html = [item for item in extra if item.artifact_variant == "original"]

    assert len(html) == 1 and html[0].kind == "html_zip"
    assert len(original_html) == 1 and original_html[0].path == package.index_path
    assert _artifact_identity(package, html[0]) != _artifact_identity(package, original_html[0])
    assert "images" not in original_html[0].path.parts


def test_researchreader_variant_is_artifact_level_idempotent(tmp_path):
    output = tmp_path / "output"
    books = tmp_path / "books"
    package_dir = output / "barrons_24-08-2026_Kobo"
    (package_dir / "images").mkdir(parents=True)
    books.mkdir()
    (package_dir / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    (package_dir / "images" / "cover.jpg").write_bytes(b"image")
    (books / "barrons 24-08-2026 (Kobo).epub").write_bytes(b"epub")
    state_path = tmp_path / "state.json"

    first = ResearchReaderScanner(output, books).scan()[0]
    first_client = FakeNotion()
    first_messages = NotionSync(first_client, "root", state_path=state_path).sync([first])
    assert first_messages == ["SYNC SUCCESS · BARRON'S · 2026-08-24"]
    assert len(first_client.uploads) == 3

    dual = package_dir / "daily-zh-CN-dual (5).html"
    dual.write_text("<html><body>双语内容</body></html>", encoding="utf-8")
    second = ResearchReaderScanner(output, books).scan()[0]
    assert second.package_key == first.package_key

    dry_messages = NotionSync(None, "root", state_path=state_path).sync([second], dry_run=True)
    assert "SYNC PLAN" in dry_messages[0]
    assert "upload 1 file(s)" in dry_messages[0]
    assert dual.name in dry_messages[0]
    assert "already synced" not in dry_messages[0]

    second_client = FakeNotion()
    sync_messages = NotionSync(second_client, "root", state_path=state_path).sync([second])
    assert sync_messages == ["SYNC SUCCESS · BARRON'S · 2026-08-24"]
    assert [item[0] for item in second_client.uploads] == [dual.name]
    assert "HTML 阅读包 · 中英双语" in str(second_client.blocks)
    assert second_client.blocks[-1][0] != "page-3"
    assert "Legacy · 历史运行" not in str(second_client.blocks)

    third_dry = NotionSync(None, "root", state_path=state_path).sync([second], dry_run=True)
    assert "already synced" in third_dry[0]

    dual.write_text("<html><body>修改后的双语内容</body></html>", encoding="utf-8")
    changed = ResearchReaderScanner(output, books).scan()[0]
    changed_dry = NotionSync(None, "root", state_path=state_path).sync([changed], dry_run=True)
    assert "SYNC PLAN" in changed_dry[0]
    assert dual.name in changed_dry[0]


def test_researchreader_old_package_key_lookup_allows_new_variant(tmp_path):
    output = tmp_path / "output"
    books = tmp_path / "books"
    package_dir = output / "barrons_24-08-2026_Kobo"
    package_dir.mkdir(parents=True)
    books.mkdir()
    (package_dir / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    (books / "barrons 24-08-2026 (Kobo).epub").write_bytes(b"epub")
    state_path = tmp_path / "state.json"
    package = ResearchReaderScanner(output, books).scan()[0]
    NotionSync(FakeNotion(), "root", state_path=state_path).sync([package])

    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = state["packages"].pop(package.package_key)
    state["packages"]["old-package-key"] = record
    state_path.write_text(json.dumps(state), encoding="utf-8")

    dual = package_dir / "daily-zh-CN-dual (5).html"
    dual.write_text("<html><body>双语内容</body></html>", encoding="utf-8")
    updated = ResearchReaderScanner(output, books).scan()[0]
    message = NotionSync(None, "root", state_path=state_path).sync([updated], dry_run=True)[0]
    assert "upload 1 file(s)" in message
    assert dual.name in message


def test_variant_pages_are_unique_and_siblings_of_run_page(tmp_path):
    output = tmp_path / "output"
    package_dir = output / "barrons_24-08-2026_Kobo"
    package_dir.mkdir(parents=True)
    (package_dir / "daily.html").write_text("<html><body>original</body></html>", encoding="utf-8")
    (package_dir / "daily-zh-CN-translation.html").write_text("<html><body>中文</body></html>", encoding="utf-8")
    (package_dir / "daily-zh-CN-dual.html").write_text("<html><body>双语</body></html>", encoding="utf-8")
    package = ResearchReaderScanner(output, tmp_path / "books").scan()[0]
    state_path = tmp_path / "state.json"
    client = FakeNotion()

    NotionSync(client, "root", state_path=state_path).sync([package])
    variant_titles = [title for _, title, _, _ in client.created]
    assert variant_titles.count("中文") == 1
    assert variant_titles.count("中英双语") == 1
    run_id = next(page_id for _, title, page_id, _ in client.created if title == "Legacy · 历史运行")
    variant_page_ids = {
        page_id for _, title, page_id, _ in client.created if title in {"中文", "中英双语"}
    }
    assert all(page_id != run_id for page_id in variant_page_ids)
    first_created = len(client.created)

    NotionSync(client, "root", state_path=state_path).sync([package])
    assert len(client.created) == first_created
    assert len(client.uploads) == 4


def test_date_page_position_is_date_descending():
    class _Client:
        timeout = 60

    client = NotionClient("token", client=_Client())
    client.child_pages = lambda _parent: [
        {"id": "old", "title": "2026-08-01"},
        {"id": "new", "title": "2026-08-25"},
        {"id": "middle", "title": "2026-08-11"},
    ]
    assert client.date_page_position("source", "2026-08-05") == {
        "type": "after_block", "after_block": {"id": "middle"}
    }
    assert client.date_page_position("source", "2026-08-30") == {"type": "page_start"}
    assert client.date_page_position("source", "invalid-date") == {"type": "page_start"}


def test_run_page_position_is_time_descending():
    class _Client:
        timeout = 60

    client = NotionClient("token", client=_Client())
    client.child_pages = lambda _parent: [
        {"id": "old", "title": "09:00:00 · morning"},
        {"id": "new", "title": "14:00:00 · afternoon"},
        {"id": "middle", "title": "11:00:00 · noon"},
    ]
    assert client.run_page_position("date", "20260825-100000") == {
        "type": "after_block", "after_block": {"id": "middle"}
    }
    assert client.run_page_position("date", "20260825-150000") == {"type": "page_start"}


def test_page_position_types_are_valid_notion_api_values():
    source = Path("src/news/notion_sync.py").read_text(encoding="utf-8")
    assert '"type": "start"' not in source
    assert '"type": "page_start"' in source
    assert '"type": "after_block"' in source


def test_legacy_page_position_names_are_normalized_to_current_api_values():
    class _Client:
        timeout = 60

        def __init__(self):
            self.payload = None

        def request(self, method, url, **kwargs):
            self.payload = kwargs["json"]
            return type("Response", (), {
                "status_code": 200,
                "json": lambda _self: {"id": "page-1"},
            })()

    client = NotionClient("token", client=_Client())
    client.child_pages = lambda _parent: []
    assert client.find_or_create_child_page("root", "title", position={"type": "start"}) == "page-1"
    assert client.client.payload["position"] == {"type": "page_start"}


def test_notion_cli_returns_nonzero_when_any_sync_fails(monkeypatch, capsys):
    args = SimpleNamespace(
        notion_token=None, root_page_id=None, export_root="data/export/portable",
        state="data/notion-sync.json", timeout=60.0, dry_run=True,
        researchreader_output=None, researchreader_books=None,
    )
    monkeypatch.setattr("news.notion_sync.run_sync", lambda **_kwargs: ["SYNC FAILED · ECO · 2026-08-26 · error"])
    assert cmd_notion_sync(args) == 1
    assert "SYNC FAILED" in capsys.readouterr().out

    monkeypatch.setattr("news.notion_sync.run_sync", lambda **_kwargs: ["SYNC SUCCESS · ECO · 2026-08-26"])
    assert cmd_notion_sync(args) == 0


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


def test_notion_client_upload_uses_configured_limit_without_stale_zip_constants(tmp_path, monkeypatch):
    """真实文件上传路径不能引用已删除的 _ZIP_LIMIT/_PART_SIZE。"""
    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeHttpClient:
        timeout = 60

        def __init__(self):
            self.sent_parts = []
            self.sent_urls = []

        def request(self, method, url, **kwargs):
            if url.endswith("/file_uploads"):
                return FakeResponse({
                    "id": "upload-1", "status": "pending",
                    "upload_url": "https://upload.example/send",
                    "complete_url": "https://upload.example/complete",
                })
            self.sent_urls.append(url)
            return FakeResponse({"status": "uploaded"})

        def post(self, url, **kwargs):
            self.sent_parts.append(kwargs.get("data"))
            self.sent_urls.append(url)
            assert "Content-Type" not in kwargs["headers"]
            assert "file" in kwargs["files"]
            return FakeResponse({"status": "uploaded"})

    monkeypatch.delenv("NOTION_MAX_UPLOAD_MB", raising=False)
    small = tmp_path / "small.bin"
    small.write_bytes(b"small")
    fake = FakeHttpClient()
    assert NotionClient("token", client=fake).upload_file(small) == "upload-1"
    assert fake.sent_parts == [None]
    assert fake.sent_urls == ["https://upload.example/send"]

    monkeypatch.setenv("NOTION_MAX_UPLOAD_MB", "0.00001")
    large = tmp_path / "large.bin"
    large.write_bytes(b"x" * 40)
    fake = FakeHttpClient()
    assert NotionClient("token", client=fake).upload_file(large) == "upload-1"
    assert len(fake.sent_parts) == 4
    assert all(part and "part_number" in part for part in fake.sent_parts)
    assert fake.sent_urls == [
        "https://upload.example/send",
        "https://upload.example/send",
        "https://upload.example/send",
        "https://upload.example/send",
        "https://upload.example/complete",
    ]


def test_state_write_retries_replace_and_cleans_temp_file(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    original_replace = os.replace
    attempts = []

    def flaky_replace(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            raise PermissionError("simulated Windows sharing violation")
        return original_replace(source, destination)

    monkeypatch.setattr("news.notion_sync.os.replace", flaky_replace)
    _write_json(state_path, {"packages": {"one": {"synced": True}}})

    assert json.loads(state_path.read_text(encoding="utf-8"))["packages"]["one"]["synced"] is True
    assert len(attempts) == 3
    assert not list(tmp_path.glob(".notion-sync-*.json"))
