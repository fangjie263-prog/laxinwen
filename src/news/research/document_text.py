"""读取 PDF / DOCX / HTML 的标题、正文与元数据（供 AI 来源识别与主题提取）。

依赖策略（不破坏已有功能、不强制新增依赖）：

- ``.docx``：复用项目已有的 ``python-docx``（``word_export`` 已依赖）；
- ``.doc``（旧版 Word 二进制）：尽力而为地抽取文本（可选 antiword / catdoc，
  否则用字节层兜底）。**任何情况下都不抛异常**——读不到内容只记 warning，
  绝不能让文件因此被忽略（需求八：``.doc`` 全流程支持）；
- ``.html`` / ``.htm``：复用项目已有的 ``selectolax``（``notion_sync`` 已依赖），
  读取 ``<title>`` / ``<meta>`` / 正文；
- ``.pdf``：**可选** ``pypdf``。未安装时不报错、不崩溃，只是返回空内容，
  此时 AI 来源会自然退化为 ``Unknown``（符合「不猜」原则）。

所有函数都保证「读不到就返回空」，绝不抛异常打断整条归档流程 —— 只有在
写归档副本 / 切割等真正需要失败的地方才抛错。
"""

from __future__ import annotations

import logging
import re
import shutil as _shutil
import subprocess as _subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

CONTENT_OK = "OK"
CONTENT_PARTIAL = "PARTIAL"
CONTENT_UNREADABLE = "UNREADABLE"

# ``.doc`` 字节兜底：连续可读片段（CJK / 拉丁字母 / 数字 / 常见标点）
_READABLE_RUN_RE = re.compile(
    r"[\w\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uff00-\uffef"
    r" :：,，.。/\-]{2,}"
)

# 内容识别最多读取的字符数（PDF 尽量多，HTML/DOCX 取头部足够）
PDF_MAX_CHARS = 60_000
DOCX_MAX_CHARS = 60_000
HTML_MAX_CHARS = 60_000

# 可选依赖可用性（只探测一次）
try:  # pragma: no cover - 依赖探测
    from pypdf import PdfReader  # type: ignore

    PYPDF_AVAILABLE = True
except Exception:  # pragma: no cover
    PdfReader = None  # type: ignore
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


@dataclass(frozen=True)
class DocumentText:
    """文档文本摘要。"""

    path: Path
    kind: str = ""
    title: str = ""
    text: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    error: str = ""
    content_status: str = CONTENT_OK

    @property
    def ok(self) -> bool:
        return self.content_status in {CONTENT_OK, CONTENT_PARTIAL} and not self.error

    @property
    def snippets(self) -> tuple[str, ...]:
        """供 AI 来源识别的候选文本片段（标题优先）。"""
        if self.content_status == CONTENT_UNREADABLE:
            return ()
        parts = [self.title, self.text]
        for value in self.metadata.values():
            if value:
                parts.append(str(value))
        return tuple(part for part in parts if part)


def _normalize_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".htm":
        return ".html"
    return suffix


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def read_pdf_text(path: str | Path, *, max_pages: int = 12) -> DocumentText:
    """读取 PDF 的元数据与前置页面文本（第一页优先，超出后再继续）。"""
    file_path = Path(path)
    if not PYPDF_AVAILABLE:
        return DocumentText(
            path=file_path, kind=".pdf",
            error="未安装可选依赖 pypdf，无法读取 PDF 内容（AI 来源将回退为 Unknown）",
            content_status=CONTENT_UNREADABLE,
        )
    try:
        reader = PdfReader(str(file_path))
        metadata: dict[str, str] = {}
        try:
            info = reader.metadata or {}
            for key in ("/Title", "/Author", "/Producer", "/Creator", "/Subject", "/Keywords"):
                value = info.get(key)
                if value:
                    metadata[key.lstrip("/").lower()] = str(value)
        except Exception as exc:  # pragma: no cover - 元数据异常不应影响主流程
            logger.debug("PDF 元数据读取失败 %s: %s", file_path, exc)

        chunks: list[str] = []
        total = 0
        page_count = len(reader.pages)
        for index in range(min(page_count, max_pages)):
            try:
                page_text = reader.pages[index].extract_text() or ""
            except Exception as exc:  # pragma: no cover
                logger.debug("PDF 第 %d 页读取失败 %s: %s", index + 1, file_path, exc)
                continue
            if page_text:
                chunks.append(page_text)
                total += len(page_text)
            if total >= PDF_MAX_CHARS:
                break
        title = metadata.get("title", "")
        text = _truncate("\n\n".join(chunks), PDF_MAX_CHARS)
        return DocumentText(
            path=file_path, kind=".pdf", title=title,
            text=text, metadata=metadata,
            content_status=CONTENT_OK if len(text.strip()) >= 8 else CONTENT_UNREADABLE,
        )
    except Exception as exc:
        return DocumentText(path=file_path, kind=".pdf", error=f"PDF 读取失败：{exc}", content_status=CONTENT_UNREADABLE)


def read_docx_text(path: str | Path) -> DocumentText:
    """读取 DOCX 的标题、正文与核心属性。"""
    file_path = Path(path)
    if not PYTHON_DOCX_AVAILABLE:
        return DocumentText(
            path=file_path, kind=".docx",
            error="未安装可选依赖 python-docx，无法读取 DOCX 内容",
            content_status=CONTENT_UNREADABLE,
        )
    try:
        document = Document(str(file_path))
        paragraphs = [para.text for para in document.paragraphs if para.text and para.text.strip()]
        metadata: dict[str, str] = {}
        try:
            props = document.core_properties
            for key in ("title", "author", "subject", "comments", "last_modified_by", "category", "keywords"):
                value = getattr(props, key, None)
                if value:
                    metadata[key] = str(value)
        except Exception as exc:  # pragma: no cover
            logger.debug("DOCX 核心属性读取失败 %s: %s", file_path, exc)
        title = metadata.get("title", "") or (paragraphs[0] if paragraphs else "")
        return DocumentText(
            path=file_path, kind=".docx", title=title,
            text=_truncate("\n\n".join(paragraphs), DOCX_MAX_CHARS), metadata=metadata,
            content_status=CONTENT_OK if paragraphs or metadata else CONTENT_PARTIAL,
        )
    except Exception as exc:
        return DocumentText(path=file_path, kind=".docx", error=f"DOCX 读取失败：{exc}", content_status=CONTENT_UNREADABLE)


DOC_MAX_CHARS = 60_000


def read_doc_text(path: str | Path) -> DocumentText:
    """读取旧版 ``.doc``（OLE2 复合文档）的可见文本。

    ``.doc`` 是二进制格式，纯 Python 精确解析成本很高，因此这里采取
    **尽力而为 + 绝不失败** 的策略：

    1. 若系统装了 ``antiword`` / ``catdoc``，优先用它们；
    2. 否则从原始字节里抽取 UTF-16LE 与 GBK 可读文本片段（够用于
       AI 来源 / 主题识别）；
    3. 任何情况下都返回 ``DocumentText``，**不抛异常** ——
       文本提取失败绝不能导致文件被忽略（需求八）。
    """
    file_path = Path(path)

    # 1) 可选外部转换器
    for tool in ("antiword", "catdoc"):
        executable = _shutil.which(tool)
        if not executable:
            continue
        try:
            completed = _subprocess.run(
                [executable, str(file_path)],
                capture_output=True, timeout=30, check=False,
            )
        except Exception as exc:  # pragma: no cover - 外部工具异常
            logger.debug("%s 读取 .doc 失败 %s: %s", tool, file_path, exc)
            continue
        if completed.returncode == 0:
            text = completed.stdout.decode("utf-8", errors="replace").strip()
            if len(text) >= 8:
                first_line = text.splitlines()[0].strip()
                return DocumentText(
                    path=file_path, kind=".doc",
                    title=_truncate(first_line, 200),
                    text=_truncate(text, DOC_MAX_CHARS),
                    metadata={"reader": tool}, content_status=CONTENT_OK,
                )

    # 2) 字节层兜底：抽取可读片段
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        return DocumentText(path=file_path, kind=".doc", error=f"DOC 读取失败：{exc}", content_status=CONTENT_UNREADABLE)

    chunks: list[str] = []
    for encoding in ("utf-16-le", "gbk", "latin-1"):
        try:
            chunks.append(raw.decode(encoding, errors="ignore"))
        except Exception:  # pragma: no cover - 解码兜底
            continue

    runs: list[str] = []
    for blob in chunks:
        for run in _READABLE_RUN_RE.findall(blob):
            cleaned = run.strip()
            if len(cleaned) >= 2:
                runs.append(cleaned)
    text = _truncate(" ".join(runs), DOC_MAX_CHARS)
    if not text:
        return DocumentText(
            path=file_path, kind=".doc",
            error=".doc 为二进制格式，未提取到文本（不影响扫描 / 归档 / 上传）",
            content_status=CONTENT_UNREADABLE,
        )
    return DocumentText(
        path=file_path, kind=".doc", text=text, metadata={"reader": "bytes-fallback"},
        error=".doc 仅提取到字节层片段，内容不可靠（仅用于诊断）",
        content_status=CONTENT_UNREADABLE,
    )


def read_html_text(path: str | Path) -> DocumentText:
    """读取 HTML 的 ``<title>`` / ``<meta>`` 与正文文本。"""
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return DocumentText(path=file_path, kind=".html", error=f"HTML 读取失败：{exc}", content_status=CONTENT_UNREADABLE)

    if not SELECTOLAX_AVAILABLE:
        # 没有 selectolax 时用正则兜底，保证 AI 识别仍可用
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        return DocumentText(
            path=file_path, kind=".html",
            title=_truncate(title_match.group(1), 400) if title_match else "",
            text=_truncate(body, HTML_MAX_CHARS),
        )

    try:
        tree = HTMLParser(raw)
        title = ""
        node = tree.css_first("title")
        if node is not None:
            title = node.text(strip=True) or ""
        metadata: dict[str, str] = {}
        for meta in tree.css("meta"):
            name = (meta.attributes.get("name") or meta.attributes.get("property") or "").strip().lower()
            content = (meta.attributes.get("content") or "").strip()
            if name and content:
                metadata[name] = content
        body_node = tree.css_first("body")
        body_text = body_node.text(separator="\n", strip=True) if body_node is not None else tree.text(separator="\n", strip=True)
        return DocumentText(
            path=file_path, kind=".html", title=title,
            text=_truncate(body_text or "", HTML_MAX_CHARS), metadata=metadata,
        )
    except Exception as exc:
        return DocumentText(path=file_path, kind=".html", error=f"HTML 解析失败：{exc}", content_status=CONTENT_UNREADABLE)


def read_document_text(path: str | Path) -> DocumentText:
    """按扩展名分派读取；未知扩展名返回空结果（不报错）。"""
    file_path = Path(path)
    kind = _normalize_kind(file_path)
    if kind == ".pdf":
        return read_pdf_text(file_path)
    if kind == ".docx":
        return read_docx_text(file_path)
    if kind == ".doc":
        return read_doc_text(file_path)
    if kind == ".html":
        return read_html_text(file_path)
    return DocumentText(path=file_path, kind=kind, error=f"不支持的扩展名：{kind or '(none)'}", content_status=CONTENT_UNREADABLE)


def read_documents_text(paths: Iterable[str | Path]) -> list[DocumentText]:
    """批量读取（每份失败都不影响其它文件）。"""
    return [read_document_text(path) for path in paths]
