"""导出层 —— 从 SQLite（唯一事实来源）派生出 JSONL / Markdown 文件。

正确关系：
    SQLite → JSONL 导出
    SQLite → Markdown 导出
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .model import Article
from .storage import Storage

logger = logging.getLogger(__name__)


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def export_jsonl(
    storage: Storage,
    out_path: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int | None = None,
) -> int:
    """导出 JSONL（每行一篇新闻）。返回导出篇数。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    articles = storage.list_articles(
        source_id=source_id, limit=limit if limit else 10**9
    )
    with out_path.open("w", encoding="utf-8") as fh:
        for art in articles:
            fh.write(json.dumps(art.to_dict(), ensure_ascii=False) + "\n")
    logger.info("导出 JSONL: %s（%d 篇）", out_path, len(articles))
    return len(articles)


def _md_frontmatter(art: Article) -> str:
    authors = ", ".join(art.authors) if art.authors else ""
    lines = [
        "---",
        f"title: {art.title}",
        f"source: {art.source_name}",
        f"published_at: {_fmt_dt(art.published_at)}",
        f"discovered_at: {_fmt_dt(art.discovered_at)}",
    ]
    if authors:
        lines.append(f"author: {authors}")
    if art.language:
        lines.append(f"language: {art.language}")
    lines.append(f"url: {art.canonical_url}")
    lines.append("---")
    return "\n".join(lines)


def export_markdown(
    storage: Storage,
    out_dir: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int | None = None,
) -> int:
    """导出 Markdown 到 out_dir/YYYY/MM/<index>-<slug>.md。返回导出篇数。"""
    out_dir = Path(out_dir)
    articles = storage.list_articles(
        source_id=source_id, limit=limit if limit else 10**9
    )
    for i, art in enumerate(articles, start=1):
        published = art.published_at or art.discovered_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)
        month_dir = out_dir / f"{published:%Y}" / f"{published:%m}"
        month_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(art.title) or f"article-{art.id or i}"
        path = month_dir / f"{i:04d}-{slug}.md"
        body = art.body_text or ""
        content = f"{_md_frontmatter(art)}\n\n{body}\n"
        path.write_text(content, encoding="utf-8")
    logger.info("导出 Markdown: %s（%d 篇）", out_dir, len(articles))
    return len(articles)


def _slugify(text: str, max_len: int = 60) -> str:
    """生成文件名字段（移除路径不安全字符）。"""
    import re
    import unicodedata

    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    norm = norm.replace(" ", "-")
    norm = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff\u3040-\u30ff\u00c0-\u024f-]", "", norm)
    norm = re.sub(r"-+", "-", norm).strip("-")
    return norm[:max_len]
