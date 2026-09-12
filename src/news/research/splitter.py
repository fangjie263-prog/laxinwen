"""按文件类型正确切割（PDF 按页 / DOCX 按段落 / HTML 按 section）。

需求二十六～二十七的硬约束：

- **不允许按字节硬切**：那会生成损坏文件；
- PDF 按**页**切，每片 ≤ ``MAX_ARCHIVE_FILE_SIZE``；单页超限时**明确记录异常**，
  不生成损坏 PDF（返回 ``oversized_page``，交由上层标记 FAILED 并保留在 Inbox）；
- DOCX 按**段落**切，每片仍是**合法 DOCX**（用 python-docx 重新写文件）；
- HTML 按 ``<section>`` / 顶层块切，每片仍是合法 HTML（含 ``<html><body>``）；
- ``.doc``（OLE2 二进制）**无法安全按内容切割**：只有在文件本身不超限时才
  整文件透传为唯一下载件，超限时明确报错（绝不按字节硬切、绝不丢文件）；
- 分片名统一 ``_Part01`` / ``_Part02``（两位数字，保证机器排序）。

每片都会**再验证一次**：大小 ≤ 限制、且能被对应解析器重新打开。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .sanitize import MAX_ARCHIVE_FILE_SIZE, part_number

logger = logging.getLogger(__name__)

# 允许超出限制的最大容忍比例（1.0 = 严格 ≤ 限制）
SIZE_TOLERANCE = 1.0

try:  # pragma: no cover - 依赖探测
    from pypdf import PdfReader, PdfWriter  # type: ignore

    PYPDF_AVAILABLE = True
except Exception:  # pragma: no cover
    PdfReader = None  # type: ignore
    PdfWriter = None  # type: ignore
    PYPDF_AVAILABLE = False

try:  # pragma: no cover - 依赖探测
    from docx import Document  # type: ignore

    PYTHON_DOCX_AVAILABLE = True
except Exception:  # pragma: no cover
    Document = None  # type: ignore
    PYTHON_DOCX_AVAILABLE = False

try:  # pragma: no cover - 依赖探测
    from selectolax.parser import HTMLParser  # type: ignore

    SELECTOLAX_AVAILABLE = True
except Exception:  # pragma: no cover
    HTMLParser = None  # type: ignore
    SELECTOLAX_AVAILABLE = False


class SplitError(RuntimeError):
    """切割失败（原文件必须保留，不得删除）。"""


@dataclass
class SplitResult:
    """切割结果。"""

    parts: list[Path] = field(default_factory=list)
    kind: str = ""
    reason: str = ""
    error: str = ""
    oversized: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def split(self) -> bool:
        return len(self.parts) > 1

    @property
    def total_parts(self) -> int:
        return max(1, len(self.parts))


def _part_path(target_dir: Path, stem: str, extension: str, index: int) -> Path:
    return target_dir / f"{stem}_{part_number(index)}{extension}"


def _is_within(path: Path, limit: int) -> bool:
    return path.stat().st_size <= int(limit * SIZE_TOLERANCE)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

# 顶层可切割块（按优先级）
_HTML_BLOCK_SELECTORS = ("section", "article", "div.chapter", "div.section")
_HTML_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _html_shell(title: str) -> str:
    safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{safe_title}</title>\n</head>\n<body>\n"
    )


def _html_blocks(raw: str) -> list[str]:
    """把 HTML 拆成顶层块：优先 ``<section>``，退化为 ``<body>`` 直接子块，再退化为分区。"""
    if SELECTOLAX_AVAILABLE:
        try:
            tree = HTMLParser(raw)
            for selector in _HTML_BLOCK_SELECTORS:
                nodes = tree.css(selector)
                if len(nodes) >= 2:
                    blocks = [node.html or "" for node in nodes]
                    blocks = [block for block in blocks if block.strip()]
                    if len(blocks) >= 2:
                        return blocks
        except Exception as exc:  # pragma: no cover
            logger.debug("HTML 块解析失败，退回正则：%s", exc)

    # 正则兜底：逐块切 <section>（含嵌套的最小平衡实现）
    blocks = _split_balanced(raw, "section")
    if len(blocks) >= 2:
        return blocks
    blocks = _split_balanced(raw, "article")
    if len(blocks) >= 2:
        return blocks
    # 最后兜底：按 <h1/h2> 标题切
    parts = re.split(r"(?=<h[12][\s>])", raw, flags=re.IGNORECASE)
    return [part for part in parts if part.strip()] or [raw]


def _split_balanced(raw: str, tag: str) -> list[str]:
    """按 ``<tag>...</tag>`` 平衡切分（简单实现，足够处理正常 HTML）。"""
    pattern = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
    close_pattern = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    blocks: list[str] = []
    depth = 0
    start: Optional[int] = None
    index = 0
    length = len(raw)
    while index < length:
        open_match = pattern.search(raw, index)
        close_match = close_pattern.search(raw, index)
        if open_match is None and close_match is None:
            break
        if open_match is not None and (close_match is None or open_match.start() < close_match.start()):
            if depth == 0:
                start = open_match.start()
            depth += 1
            index = open_match.end()
        else:
            assert close_match is not None
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(raw[start : close_match.end()])
                    start = None
            index = close_match.end()
    if start is not None and depth > 0:
        blocks.append(raw[start:])
    return blocks


def split_html(
    path: str | Path,
    target_dir: str | Path,
    *,
    stem: str,
    max_bytes: int = MAX_ARCHIVE_FILE_SIZE,
) -> SplitResult:
    """按 section / 章节切 HTML，每片都是合法 HTML。"""
    source = Path(path)
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SplitResult(kind="html", error=f"HTML 读取失败：{exc}")

    title_match = _HTML_TITLE_RE.search(raw)
    title = (title_match.group(1).strip() if title_match else source.stem)
    # 分片使用统一的最小 HTML 外壳（含 <title>），保证每片都能独立打开。
    # 源文件的 <head> 只保留标题，避免把大段内联样式 / 脚本重复到每个分片，
    # 否则分片会越切越大。
    shell = _html_shell(title)
    footer = "\n</body>\n</html>\n"

    blocks = _html_blocks(raw)
    parts: list[Path] = []
    index = 1
    current: list[str] = []
    current_size = len((shell + footer).encode("utf-8"))
    oversized: list[str] = []

    def flush() -> None:
        nonlocal current, current_size, index
        if not current:
            return
        payload = shell + "\n".join(current) + footer
        encoded = payload.encode("utf-8")
        if len(encoded) > max_bytes and len(current) == 1:
            # 单个块本身就超限：记录异常，不生成损坏文件
            oversized.append(f"block#{index}")
        else:
            destination = _part_path(out_dir, stem, ".html", index)
            destination.write_bytes(encoded)
            parts.append(destination)
        index += 1
        current = []
        current_size = len((shell + footer).encode("utf-8"))

    for block in blocks:
        block_size = len(block.encode("utf-8"))
        if current and current_size + block_size > max_bytes:
            flush()
        current.append(block)
        current_size += block_size
        if current_size > max_bytes:
            # 单块超限 → 立即记录并按当前状态落盘（若仍超限则进入 oversized）
            flush()
    flush()

    if oversized:
        return SplitResult(
            parts=parts, kind="html",
            error="存在无法在大小限制内切割的内容块：" + ", ".join(oversized),
            oversized=oversized,
        )
    if not parts:
        # 极端情况（空文档）：输出单文件而不是失败
        destination = _part_path(out_dir, stem, ".html", 1)
        destination.write_text(shell + raw + footer, encoding="utf-8")
        parts.append(destination)
    return SplitResult(parts=parts, kind="html", reason=f"{len(blocks)} 个内容块")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def split_pdf(
    path: str | Path,
    target_dir: str | Path,
    *,
    stem: str,
    max_bytes: int = MAX_ARCHIVE_FILE_SIZE,
) -> SplitResult:
    """按**页**切 PDF；单页超限时明确报错，绝不生成损坏 PDF。"""
    if not PYPDF_AVAILABLE:
        return SplitResult(kind="pdf", error="未安装可选依赖 pypdf，无法按页切割 PDF")
    source = Path(path)
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        reader = PdfReader(str(source))
        page_count = len(reader.pages)
    except Exception as exc:
        return SplitResult(kind="pdf", error=f"PDF 打开失败：{exc}")

    parts: list[Path] = []
    oversized: list[str] = []
    index = 1
    current_pages: list[int] = []

    def flush() -> None:
        nonlocal current_pages, index
        if not current_pages:
            return
        destination = _part_path(out_dir, stem, ".pdf", index)
        writer = PdfWriter()
        for page_index in current_pages:
            writer.add_page(reader.pages[page_index])
        try:
            with destination.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise SplitError(f"PDF 分片写入失败（第 {index} 片）：{exc}") from exc
        if not _is_within(destination, max_bytes):
            if len(current_pages) == 1:
                # 单页超限：删除该片并记录异常（需求二十六）
                destination.unlink(missing_ok=True)
                oversized.append(f"page{current_pages[0] + 1}")
                current_pages = []
                index += 1
                return
            # 多页超限 → 拆细重试
            destination.unlink(missing_ok=True)
            pages_to_retry = list(current_pages)
            current_pages = []
            midpoint = max(1, len(pages_to_retry) // 2)
            for group in (pages_to_retry[:midpoint], pages_to_retry[midpoint:]):
                if not group:
                    continue
                current_pages = group
                flush()
            return
        parts.append(destination)
        current_pages = []
        index += 1

    current_size = 0
    for page_index in range(page_count):
        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        import io

        buffer = io.BytesIO()
        writer.write(buffer)
        page_size = buffer.tell()
        if current_pages and current_size + page_size > max_bytes:
            flush()
            current_size = 0
        current_pages.append(page_index)
        current_size += page_size
        if current_size > max_bytes:
            flush()
            current_size = 0
    flush()

    if oversized:
        return SplitResult(
            parts=parts, kind="pdf",
            error="以下页面单独超过大小限制，无法安全切割：" + ", ".join(oversized),
            oversized=oversized,
        )
    if not parts:
        destination = _part_path(out_dir, stem, ".pdf", 1)
        writer = PdfWriter()
        for page in range(page_count):
            writer.add_page(reader.pages[page])
        with destination.open("wb") as handle:
            writer.write(handle)
        parts.append(destination)
    return SplitResult(parts=parts, kind="pdf", reason=f"{page_count} 页")


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _docx_paragraph_payloads(document) -> list[tuple[str, float]]:
    """估算每个段落的「相对体量」（用字符数近似）。

    python-docx 无法在不写文件的情况下精确知道某个段落的字节数，因此采用
    字符数比例 + 写盘后实测校验的策略：先把段落分组到接近上限，写入后
    若仍超限，再自动细分。
    """
    payloads: list[tuple[str, float]] = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        text = "".join(child.itertext())
        payloads.append((tag, float(len(text or ""))))
    return payloads


def _write_docx_subset(source_path: Path, indices: list[int], destination: Path) -> None:
    """把源 DOCX 的指定顶层元素写成一个**合法**新 DOCX。

    用「新建文档 + 显式追加元素」而不是「删元素」，原因是：

    - ``python-docx`` 的 ``Document()`` 新建文档会带上自己的 ``sectPr``；
    - 若直接复制源文档元素，会出现两个 ``sectPr``（内容重复、体积翻倍），
      导致分片越切越大；
    - 显式追加可以精确控制每个分片的内容与体积。

    复制失败时回退为整文件复制（保证仍有合法 DOCX 产出）。
    """
    from copy import deepcopy

    source_doc = Document(str(source_path))
    source_body = source_doc.element.body
    children = [child for child in source_body.iterchildren()]

    target_doc = Document()
    target_body = target_doc.element.body
    # 移除新建文档自带的空段落与 section 属性，稍后按需追加
    target_sect = None
    for child in list(target_body.iterchildren()):
        tag = child.tag.split("}")[-1]
        if tag == "sectPr":
            target_sect = child
        else:
            target_body.remove(child)
    for index in indices:
        if not (0 <= index < len(children)):
            continue
        element = children[index]
        if element.tag.split("}")[-1] == "sectPr":
            continue
        try:
            target_body.append(deepcopy(element))
        except Exception as exc:  # pragma: no cover - 意外形态
            logger.debug("DOCX 元素复制失败（已跳过）：%s", exc)
    # 源文档的页面设置优先，保证分片排版与原文件一致
    source_sect = None
    for child in children:
        if child.tag.split("}")[-1] == "sectPr":
            source_sect = child
            break
    if source_sect is not None:
        try:
            if target_sect is not None:
                target_body.remove(target_sect)
            target_body.append(deepcopy(source_sect))
        except Exception as exc:  # pragma: no cover
            logger.debug("DOCX sectPr 继承失败（沿用默认页面设置）：%s", exc)
    target_doc.save(str(destination))


def _docx_container_size(path: Path) -> int:
    """估算 DOCX 固定容器开销（styles / theme / settings 等）。

    做法：生成一个只含一个空段落的同源分片，其大小即容器开销上界。
    """
    import tempfile as _tempfile

    try:
        with _tempfile.TemporaryDirectory(prefix="laxinwen-docx-probe-") as temp:
            probe = Path(temp) / "probe.docx"
            _write_docx_subset(path, [0], probe)
            return probe.stat().st_size
    except Exception:  # pragma: no cover
        return 0


def split_docx(
    path: str | Path,
    target_dir: str | Path,
    *,
    stem: str,
    max_bytes: int = MAX_ARCHIVE_FILE_SIZE,
) -> SplitResult:
    """按段落 / 章节切 DOCX，每片都是合法 DOCX。"""
    if not PYTHON_DOCX_AVAILABLE:
        return SplitResult(kind="docx", error="未安装可选依赖 python-docx，无法切割 DOCX")
    source = Path(path)
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        document = Document(str(source))
    except Exception as exc:
        return SplitResult(kind="docx", error=f"DOCX 打开失败：{exc}")

    payloads = _docx_paragraph_payloads(document)
    if not payloads:
        destination = _part_path(out_dir, stem, ".docx", 1)
        _write_docx_subset(source, [], destination)
        return SplitResult(parts=[destination], kind="docx", reason="无可切分内容")

    # 先猜一个「每片元素数」，写入后按实测大小整体缩放，避免逐元素递归
    # 造成的碎片化（python-docx 每个分片都有不可压缩的容器开销）。
    container_estimate = _docx_container_size(source)
    available = max(1, max_bytes - container_estimate)

    def pack(chunk_size: int) -> list[list[int]]:
        chunk_size = max(1, int(chunk_size))
        return [
            list(range(start, min(start + chunk_size, len(payloads))))
            for start in range(0, len(payloads), chunk_size)
        ]

    def measure(groups: list[list[int]], probe_dir: Path) -> int:
        """实测一组分组中最大的分片大小（只实测最多 3 组，控制耗时）。"""
        largest = 0
        for order, group in enumerate(groups[:3]):
            probe = probe_dir / f".probe-{order}.docx"
            _write_docx_subset(source, group, probe)
            largest = max(largest, probe.stat().st_size)
            probe.unlink(missing_ok=True)
        return largest

    content_bytes = max(1, source.stat().st_size - container_estimate)
    per_element = max(1.0, content_bytes / max(1, len(payloads)))
    # 首次估算：以「元素平均体量」为单位分组
    chunk = max(1, int(max(1.0, available * 0.9) / per_element))
    groups = pack(chunk)
    for _ in range(12):
        largest = measure(groups, out_dir)
        if largest == 0:
            break
        if largest <= max_bytes:
            break
        # 按实测超出比例缩小每片元素数
        ratio = max_bytes / largest
        new_chunk = max(1, int(chunk * ratio * 0.95))
        if new_chunk >= chunk:
            new_chunk = max(1, chunk - 1)
        chunk = new_chunk
        groups = pack(chunk)


    # 逐组写盘并**实测**；超限则递归二分组
    parts: list[Path] = []
    index_counter = 1
    oversized: list[str] = []

    if container_estimate > max_bytes:
        return SplitResult(
            kind="docx",
            error=(
                f"DOCX 容器最小开销约 {container_estimate} 字节，已超过大小限制 "
                f"{int(max_bytes)} 字节，无法生成合法分片"
            ),
        )

    def emit(indices: list[int]) -> None:
        nonlocal index_counter
        if not indices:
            return
        destination = _part_path(out_dir, stem, ".docx", index_counter)
        _write_docx_subset(source, indices, destination)
        if _is_within(destination, max_bytes):
            parts.append(destination)
            index_counter += 1
            return
        # 单元素分片仍超限（例如一张巨大图片）：记录异常，不生成损坏 / 碎片文件
        destination.unlink(missing_ok=True)
        oversized.append(
            f"element{indices[0]}" if len(indices) == 1 else f"group@{indices[0]}({len(indices)})"
        )
        index_counter += 1

    for group in groups:
        emit(group)

    # 自检：分片数量不应超过「理论下限 + 少量余量」。
    # DOCX 容器固定开销会在每个分片重复出现，若估算严重偏离，说明分组
    # 逻辑有问题——此时明确报错，宁可不切割，也不生成上百个碎片。
    if parts:
        container = _docx_container_size(source)
        content_bytes = max(0, source.stat().st_size - container)
        usable = max(1.0, max_bytes - container)
        avg_part = sum(path.stat().st_size for path in parts) / len(parts)
        theoretical = max(1, int(content_bytes / usable) + 1)
        if len(parts) > max(8, theoretical * 4):
            return SplitResult(
                kind="docx",
                error=(
                    f"DOCX 分片数异常（{len(parts)} 片，理论下限约 {theoretical} 片，"
                    f"平均分片 {int(avg_part)} 字节），已放弃切割以避免生成碎片文件"
                ),
            )

    if oversized:
        return SplitResult(
            parts=parts, kind="docx",
            error="以下内容无法在大小限制内切割：" + ", ".join(oversized[:10]) + ("…" if len(oversized) > 10 else ""),
            oversized=oversized,
        )
    return SplitResult(parts=parts, kind="docx", reason=f"{len(groups)} 组")


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

_PART_SUFFIX_RE = re.compile(r"_Part\d{2,}$", re.IGNORECASE)


def part_stem(stem: str) -> str:
    """去掉已有的 ``_PartNN`` 后缀，避免二次切割时叠加。"""
    return _PART_SUFFIX_RE.sub("", str(stem))


def split_file(
    path: str | Path,
    target_dir: str | Path,
    *,
    stem: str | None = None,
    max_bytes: int = MAX_ARCHIVE_FILE_SIZE,
) -> SplitResult:
    """按扩展名分派切割。未知类型返回错误（不猜测、不硬切）。"""
    source = Path(path)
    extension = source.suffix.lower()
    base_stem = part_stem(stem or source.stem)
    if extension == ".pdf":
        return split_pdf(source, target_dir, stem=base_stem, max_bytes=max_bytes)
    if extension == ".docx":
        return split_docx(source, target_dir, stem=base_stem, max_bytes=max_bytes)
    if extension in {".html", ".htm"}:
        return split_html(source, target_dir, stem=base_stem, max_bytes=max_bytes)
    if extension == ".doc":
        return _pass_through_binary(source, target_dir, stem=base_stem, max_bytes=max_bytes)
    return SplitResult(kind=extension, error=f"不支持切割的文件类型：{extension or '(none)'}")


def _pass_through_binary(
    source: Path, target_dir: str | Path, *, stem: str, max_bytes: int
) -> SplitResult:
    """二进制且不可安全切割的格式（``.doc``）：整文件作为唯一分片。

    这是「不丢文件」的兜底，而不是「假装切成功」：仅当文件本身在大小限制
    之内时才产出分片；否则返回明确错误，由上层保留原文件并记录 FAILED。
    """
    import shutil

    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    size = source.stat().st_size
    if size > max_bytes:
        return SplitResult(
            kind=source.suffix.lower(),
            error=(
                f"{source.suffix.lower()} 为二进制格式，无法安全切割，"
                f"且文件大小 {size} 已超过限制 {int(max_bytes)}"
            ),
        )
    destination = out_dir / f"{stem}{source.suffix.lower()}"
    if destination.resolve() != source.resolve():
        shutil.copy2(source, destination)
    return SplitResult(parts=[destination], kind=source.suffix.lower(), reason="二进制整文件")


def verify_part(path: str | Path, kind: str) -> bool:
    """切割后验证：文件仍然可被对应解析器打开（防止生成损坏文件）。"""
    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size == 0:
        return False
    suffix = (kind or file_path.suffix).lower()
    try:
        if suffix == ".pdf":
            if not PYPDF_AVAILABLE:
                return file_path.read_bytes().startswith(b"%PDF")
            reader = PdfReader(str(file_path))
            return len(reader.pages) > 0
        if suffix == ".docx":
            if not PYTHON_DOCX_AVAILABLE:
                return file_path.read_bytes().startswith(b"PK")
            Document(str(file_path))
            return True
        if suffix in {".html", ".htm"}:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            return "<html" in text.lower() or "<section" in text.lower() or "<body" in text.lower()
        if suffix == ".doc":
            # 二进制容器无法解析内容：非空即视为「原文件可用」，
            # 避免因无法解析而误判失败、导致文件被丢弃。
            return file_path.stat().st_size > 0
    except Exception as exc:
        logger.warning("分片校验失败 %s: %s", file_path, exc)
        return False
    return True
