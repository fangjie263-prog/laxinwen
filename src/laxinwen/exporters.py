"""Markdown / JSONL exporters (derived files — SQLite stays the source of truth)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .model import Article


def _published_str(article: Article | dict) -> str:
    val = article.published_at_iso() if isinstance(article, Article) else article.get("published_at")
    return val or ""


def article_to_markdown(article: Article) -> str:
    """Render a single Article as human-readable Markdown."""
    published = _published_str(article)
    authors = ", ".join(article.authors) or "Unknown"
    md = ["---", f"title: {article.title}", f"source: {article.source_name}"]
    if published:
        md.append(f"published_at: {published}")
    md.append(f"author: {authors}")
    md.append(f"url: {article.canonical_url}")
    md.append("---")
    md.append("")
    md.append(article.body_text or "")
    return "\n".join(md)


def export_markdown(
    articles: list[Article | dict],
    out_dir: str | Path,
    *,
    filename: str | None = None,
) -> list[Path]:
    """Export articles to Markdown files organized as ``YYYY/MM/``."""
    out_dir = Path(out_dir)
    written: list[Path] = []
    for article in articles:
        published = _published_str(article)
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        else:
            dt = None
        if dt is None:
            dt = datetime.now()
        target_dir = out_dir / f"{dt.year:04d}" / f"{dt.month:02d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        # Safe filename from the article URL slug.
        slug = (article.canonical_url if isinstance(article, Article) else article.get("canonical_url", "")).rstrip("/").split("/")[-1]
        if not slug or len(slug) > 120:
            slug = f"article-{abs(hash(slug or 'x'))}"
        safe_slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug).strip("-")
        path = target_dir / f"{safe_slug}.md"
        content = article_to_markdown(article) if isinstance(article, Article) else _dict_to_markdown(article)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _dict_to_markdown(article: dict) -> str:
    md = [
        "---",
        f"title: {article.get('title', '')}",
        f"source: {article.get('source_name', '')}",
    ]
    if article.get("published_at"):
        md.append(f"published_at: {article['published_at']}")
    md.append(f"author: {', '.join(article.get('authors', []) or []) or 'Unknown'}")
    md.append(f"url: {article.get('canonical_url', '')}")
    md.append("---")
    md.append("")
    md.append(article.get("body_text", "") or "")
    return "\n".join(md)


def export_jsonl(
    articles: list[Article | dict],
    out_path: str | Path,
) -> Path:
    """Export articles as JSONL (one JSON object per line)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for article in articles:
            data = article.to_dict() if isinstance(article, Article) else article
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")
    return out_path
