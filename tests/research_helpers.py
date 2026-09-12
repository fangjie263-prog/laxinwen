"""AI Research Archive 测试共用工具（构造最小合法 PDF / DOCX / HTML）。

放在 tests 下（而不是 conftest）是为了让测试文件可以显式导入，避免与既有
conftest 的 autouse fixture 耦合。
"""

from __future__ import annotations

from pathlib import Path

from pypdf.generic import DecodedStreamObject, NameObject


def make_pdf(path: Path, *, pages: int = 3, text: str = "天齐锂业 锂价分析", filler: int = 0) -> Path:
    """生成一个最小合法 PDF（每页都带可切割的内容流）。

    ``filler`` 会作为**每页的正文文本**写入，因此文件大小与页数成正比，
    可以精确验证「按页切割」的行为。
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    payload = (text + " " + "x" * max(0, filler))[: max(1, len(text) + filler)]
    for _ in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        if filler:
            # 标准 PDF 内容流：画一个矩形并留下文本对象，确保页对象非空
            stream = f"BT /F1 12 Tf 10 10 Td ({payload[:200]}) Tj ET".encode("latin-1", "replace")
            page[NameObject("/Contents")] = writer._add_object(DecodedStreamObject())  # type: ignore[attr-defined]
            page["/Contents"].set_data(stream + b"\n" + b"0 0 0 rg " + b"0 0 m " * 2000)
    writer.add_metadata({"/Title": text, "/Producer": "laxinwen-test"})
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def make_docx(path: Path, *, chapters: int = 3, paragraphs: int = 3, text: str = "锂价分析正文", title: str = "") -> Path:
    """生成一个最小合法 DOCX。"""
    from docx import Document

    document = Document()
    if title:
        document.add_heading(title, level=0)
    for chapter in range(chapters):
        document.add_heading(f"第 {chapter + 1} 章", level=1)
        for _ in range(paragraphs):
            document.add_paragraph(text)
    document.save(str(path))
    return path


def make_html(path: Path, *, sections: int = 3, body: str = "由 Claude 生成的研究正文", title: str = "研究") -> Path:
    """生成一个含多个 section 的 HTML。"""
    blocks = "".join(f"<section><h2>章节 {i + 1}</h2><p>{body}</p></section>" for i in range(sections))
    path.write_text(
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>{blocks}</body></html>",
        encoding="utf-8",
    )
    return path


def write_bytes(path: Path, size: int, *, prefix: bytes = b"data") -> Path:
    """写入指定大小的文件（用于大小 / 切割测试）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(prefix)
        remaining = max(0, size - len(prefix))
        chunk = b"x" * min(1024 * 1024, max(1, remaining))
        while remaining > 0:
            handle.write(chunk[:remaining])
            remaining -= len(chunk)
    return path
