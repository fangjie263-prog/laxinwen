"""Article body extraction with Trafilatura (falling back to selectolax)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import trafilatura
from selectolax.parser import HTMLParser

from .model import Article

logger = logging.getLogger(__name__)

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Normalize trailing "Z" to an explicit +00:00 offset.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_article(
    source_id: str,
    source_name: str,
    url: str,
    html: str,
    *,
    site_extract: dict | None = None,
) -> Article:
    """Extract a normalized Article from article HTML using Trafilatura."""
    site_extract = site_extract or {}

    article = Article(
        source_id=source_id,
        source_name=source_name,
        canonical_url=url,
    )

    tree = HTMLParser(html)
    node = tree.css_first('link[rel="canonical"]')
    if node:
        href = node.attributes.get("href")
        if href:
            article.canonical_url = urljoin(url, href)

    # --- title ---
    og_title = tree.css_first('meta[property="og:title"]')
    h1 = tree.css_first("h1")
    if og_title and og_title.attributes.get("content"):
        article.title = og_title.attributes["content"].strip()
    elif h1:
        article.title = (h1.text() or "").strip()

    # --- published time (article:published_time / og / time tag / JSON-LD) ---
    for selector in (
        'meta[property="article:published_time"]',
        'meta[property="og:published_time"]',
        'meta[name="date"]',
        'meta[name="parsely-pub-date"]',
        'time[datetime]',
    ):
        node = tree.css_first(selector)
        if node:
            val = node.attributes.get("content") or node.attributes.get("datetime")
            dt = _parse_datetime(val)
            if dt:
                article.published_at = dt
                break

    # --- authors (rel=author links first, then meta, then JSON-LD) ---
    authors: list[str] = []
    for node in tree.css("a[rel=author]"):
        val = (node.text() or "").strip()
        if val and val not in authors:
            authors.append(val)
    if not authors:
        for sel in ('meta[name="author"]', "address.author"):
            node = tree.css_first(sel)
            if node:
                val = node.attributes.get("content") or node.text() or ""
                val = val.strip()
                if val and val not in authors:
                    authors.append(val)
    if not authors:
        # JSON-LD Person names
        for script in tree.css('script[type="application/ld+json"]'):
            import json

            try:
                data = json.loads(script.text() or "")
            except json.JSONDecodeError:
                continue
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") in ("NewsArticle", "Article", "ReportageNewsArticle"):
                    author = item.get("author")
                    if isinstance(author, dict):
                        candidates = [author]
                    elif isinstance(author, list):
                        candidates = [a for a in author if isinstance(a, dict)]
                    else:
                        candidates = []
                    for a in candidates:
                        name = (a.get("name") or "").strip()
                        if name and name not in authors:
                            authors.append(name)
    article.authors = authors[:5]

    # --- body via Trafilatura ---
    body_text = ""
    body_html = None
    try:
        body_text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            output_format="txt",
        )
        if body_text:
            body_text = body_text.strip()
        body_html = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            output_format="html",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trafilatura failed for %s: %s", url, exc)
        body_text = ""
        body_html = None

    # Site-level cleanup: drop lines that match known page-chrome phrases
    # (e.g. "Escolha o ECO como fonte preferida no Google").
    exclude_phrases = [p for p in site_extract.get("exclude_phrases", []) or [] if p]
    if exclude_phrases:
        body_text = _strip_lines_with_phrases(body_text, exclude_phrases)
        if body_html:
            body_html = _strip_html_phrases(body_html, exclude_phrases)

    # Fallback: minimal body from selectolax if Trafilatura returned nothing.
    if not body_text:
        candidates = [sel for sel in site_extract.get("body_selectors", [])]
        if not candidates:
            candidates = [
                "article .entry__content",
                "article .entry-content",
                ".entry__content",
                ".entry-content",
                "article",
            ]
        for sel in candidates:
            node = tree.css_first(sel)
            if node:
                paragraphs = [p.text().strip() for p in node.css("p") if p.text().strip()]
                body_text = "\n\n".join(paragraphs)
                if len(body_text) > 200:
                    break
        body_text = body_text.strip()

    article.body_text = body_text or ""
    article.body_html = body_html

    # --- images ---
    images: list[str] = []
    lead = None
    og_image = tree.css_first('meta[property="og:image"]')
    if og_image and og_image.attributes.get("content"):
        lead = og_image.attributes["content"]
    for img in tree.css("article img[src]"):
        src = img.attributes.get("src", "")
        if src and src not in images:
            images.append(src)
    if lead and lead not in images:
        images.insert(0, lead)
    article.images = images
    article.lead_image = lead

    # --- language ---
    html_node = tree.css_first("html")
    if html_node and html_node.attributes.get("lang"):
        article.language = html_node.attributes["lang"]

    if body_text:
        article.status = "fetched"
    else:
        article.status = "error"
        article.errors.append("extraction: empty body")

    if article.title and body_text:
        article.status = "ok"

    return article


def _strip_lines_with_phrases(text: str, phrases: list[str]) -> str:
    """Drop lines that exactly match an excluded phrase, and also remove the
    phrase inline when it gets concatenated onto a paragraph by the extractor."""
    if not text:
        return text
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped == p or stripped.startswith(p) for p in phrases):
            continue
        for p in phrases:
            if p in line:
                line = line.replace(p, "")
        if line.strip():
            out.append(line)
    return "\n".join(out)


def _strip_html_phrases(html: str, phrases: list[str]) -> str:
    """Remove whole <p> blocks whose visible text matches an excluded phrase."""
    if not html:
        return html
    tree = HTMLParser(html)
    for node in list(tree.css("p")):
        text = (node.text() or "").strip()
        if any(text == p or text.startswith(p) for p in phrases):
            node.decompose()
    return tree.html
