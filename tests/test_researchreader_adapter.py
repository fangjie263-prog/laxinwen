from pathlib import Path

from news.integration.researchreader_adapter import ResearchReaderAdapter


def test_scan_files_lists_epub_and_pdf(tmp_path: Path):
    (tmp_path / "WSJ_2026-08-26.epub").write_bytes(b"epub")
    (tmp_path / "FT_2026-08-26.PDF").write_bytes(b"pdf")
    (tmp_path / "ignore.txt").write_text("ignore", encoding="utf-8")

    files = ResearchReaderAdapter(tmp_path, tmp_path / "output").scan_files()

    assert [(item.path.name, item.kind, item.status) for item in files] == [
        ("FT_2026-08-26.PDF", "PDF", "待处理"),
        ("WSJ_2026-08-26.epub", "EPUB", "待处理"),
    ]


def test_output_directory_matches_existing_researchreader_scanner(tmp_path: Path):
    adapter = ResearchReaderAdapter(tmp_path, tmp_path / "output")

    assert adapter.get_output_path("WSJ_2026-08-26.epub") == (
        tmp_path / "output" / "the-wall-street-journal_26-08-2026_Kobo" / "daily.html"
    )


def test_extract_uses_external_runner_and_existing_reader_functions(tmp_path: Path):
    source = tmp_path / "WSJ_2026-08-26.epub"
    source.write_bytes(b"epub")
    calls = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[-1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "daily.html").write_text("<html></html>", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = ResearchReaderAdapter(tmp_path, tmp_path / "output", runner=fake_runner).extract_epub_to_html(source)

    assert result.status == "已完成"
    assert result.output_path == tmp_path / "output" / "the-wall-street-journal_26-08-2026_Kobo" / "daily.html"
    assert calls[0][0][1] == "-c"
    assert "wsj_reader.read_epub" in calls[0][0][2]
    assert "wsj_reader.save_output" in calls[0][0][2]


def test_upload_to_notion_passes_only_selected_output_package(tmp_path: Path):
    books = tmp_path / "books"
    output = tmp_path / "output"
    books.mkdir()
    output.mkdir()
    selected = books / "WSJ_2026-08-26.epub"
    selected.write_bytes(b"epub")
    for name in ("the-wall-street-journal_24-08-2026_Kobo", "the-wall-street-journal_25-08-2026_Kobo", "the-wall-street-journal_26-08-2026_Kobo"):
        folder = output / name
        folder.mkdir()
        (folder / "daily.html").write_text("<html></html>", encoding="utf-8")

    received = []

    def fake_sync(package, *, dry_run=False):
        received.append((package, dry_run))
        return [f"SYNC SUCCESS · {package.source} · {package.date}"]

    messages = ResearchReaderAdapter(books, output, notion_sync_runner=fake_sync).upload_to_notion(selected)

    assert messages == ["SYNC SUCCESS · WSJ · 2026-08-26"]
    assert len(received) == 1
    assert received[0][0].index_path == output / "the-wall-street-journal_26-08-2026_Kobo" / "daily.html"
