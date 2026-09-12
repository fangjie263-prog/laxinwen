"""Independent Huxiu (虎嗅) discovery and extraction prototype.

The selectors in this module were derived from real Huxiu HTML captured on
2026-09-12, not synthetic fixtures.  It deliberately stops at the existing
``Article`` contract: section, tags, original publisher, and ordered blocks
remain in ``HuxiuExtractionResult.metadata`` until the core schema grows.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

from ..discover import DiscoveredItem
from ..fetch import BaseFetcher
from ..model import Article
from ..normalize import canonicalize_url
from .base import SourceAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://www.huxiu.com/"
ARTICLE_URL_RE = re.compile(r"^https?://(?:www\.|m\.)?huxiu\.com/article/(\d+)\.html(?:[?#].*)?$")
_NOISE_SELECTORS = (
    "script", "style", "nav", "header", "footer", "aside",
    ".article__related-article-wrap", ".article__comment",
    ".article__reprinted-center", ".article__user-info-wrap",
)


def normalize_huxiu_url(url: str) -> str:
    """Canonicalize Huxiu desktop/mobile article URLs and tracking parameters."""
    raw = (url or "").strip()
    if raw.startswith("/"):
        raw = urljoin(BASE_URL, raw)
    parts = urlsplit(raw)
    if parts.netloc.lower() in {"m.huxiu.com", "www.huxiu.com"}:
        path = parts.path.rstrip("/") or "/"
        if re.fullmatch(r"/article/\d+\.html", path):
            return f"https://www.huxiu.com{path}"
    return canonicalize_url(raw)


def _meta(tree: HTMLParser, key: str) -> str:
    key = key.lower()
    for node in tree.css("meta"):
        if ((node.attributes.get("property") or node.attributes.get("name") or "").lower() == key):
            return (node.attributes.get("content") or "").strip()
    return ""


def _node_text(node: Node | None) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ", strip=True) if node else "").strip()


def _absolute(url: str, base_url: str) -> str:
    return urljoin(base_url, unescape((url or "").strip()))


def _parse_dt(value: str | int | float | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Huxiu's SSR dateline is a Unix timestamp in seconds.
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    from dateutil import parser as dateparser
    try:
        dt = dateparser.parse(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_content(root: Node) -> None:
    for selector in _NOISE_SELECTORS:
        for node in root.css(selector):
            node.decompose()


def _content_blocks(root: Node, base_url: str) -> tuple[list[dict[str, str]], list[str]]:
    blocks: list[dict[str, str]] = []
    images: list[str] = []
    for node in root.css("p, h2, h3, h4, blockquote, ul, ol, figure, img, iframe, video"):
        tag = node.tag
        if tag == "img":
            src = node.attributes.get("src") or node.attributes.get("data-src") or node.attributes.get("data-original") or node.attributes.get("_src") or ""
            if src:
                absolute = _absolute(src, base_url)
                images.append(absolute)
                blocks.append({"type": "image", "url": absolute})
            continue
        if tag == "figure":
            img = node.css_first("img")
            if img is not None:
                src = img.attributes.get("src") or img.attributes.get("data-src") or img.attributes.get("_src") or ""
                if src:
                    absolute = _absolute(src, base_url)
                    images.append(absolute)
                    blocks.append({"type": "image", "url": absolute})
            caption = _node_text(node.css_first("figcaption"))
            if caption:
                blocks.append({"type": "caption", "text": caption})
            continue
        text = _node_text(node)
        if not text:
            continue
        if "text-img-note" in (node.attributes.get("class") or ""):
            blocks.append({"type": "caption", "text": text})
        elif tag in {"ul", "ol"}:
            blocks.append({"type": "list", "text": text})
        elif tag == "blockquote":
            blocks.append({"type": "blockquote", "text": text})
        else:
            blocks.append({"type": "paragraph" if tag == "p" else "heading", "text": text})
    return blocks, list(dict.fromkeys(images))


@dataclass
class HuxiuExtractionResult:
    """Rich prototype result which can be mapped to the current Article model."""

    title: str | None = None
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    updated_at: datetime | None = None
    canonical_url: str = ""
    section: str | None = None
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    body_html: str = ""
    body_text: str = ""
    blocks: list[dict[str, str]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    cover_image: str | None = None
    original_source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_article(self, *, source_id: str = "huxiu", source_name: str = "虎嗅", language: str = "zh-CN") -> Article:
        return Article(
            source_id=source_id,
            source_name=source_name,
            canonical_url=normalize_huxiu_url(self.canonical_url),
            title=(self.title or "").strip(),
            body_text=self.body_text,
            body_html=self.body_html or None,
            authors=list(self.authors),
            published_at=self.published_at,
            images=list(self.images),
            lead_image=self.cover_image or (self.images[0] if self.images else None),
            language=language,
        )


def extract_huxiu_article(html: str, *, url: str = "") -> HuxiuExtractionResult:
    """Extract one real Huxiu detail page using the observed SSR DOM."""
    tree = HTMLParser(html)
    canonical = normalize_huxiu_url(_meta(tree, "og:url") or url)
    title = _node_text(tree.css_first(".article__title")) or _meta(tree, "og:title") or _node_text(tree.css_first("h1"))
    subtitle = _node_text(tree.css_first(".article__subtitle")) or _meta(tree, "description") or None
    author_nodes = tree.css(".article-author-info .author-info__username, .article__author-info-box .author-info__username")
    authors = list(dict.fromkeys(_node_text(n) for n in author_nodes if _node_text(n)))
    if not authors and _meta(tree, "author"):
        authors = [_meta(tree, "author")]
    published = _parse_dt(_meta(tree, "article:published_time"))
    time_node = tree.css_first(".article__time")
    if published is None and time_node:
        published = _parse_dt(_node_text(time_node))
    section = _node_text(tree.css_first(".article-type-channel"))
    section = re.sub(r"^频道[:：]\s*", "", section) or None
    content = tree.css_first(".article__content")
    body_html = ""
    blocks: list[dict[str, str]] = []
    images: list[str] = []
    body_text = ""
    if content is not None:
        content_copy = HTMLParser(content.html).body or HTMLParser(content.html).root
        _clean_content(content_copy)
        body_html = content_copy.html or ""
        blocks, images = _content_blocks(content_copy, canonical or url or BASE_URL)
        body_text = "\n\n".join(block["text"] for block in blocks if block["type"] in {"paragraph", "heading", "blockquote", "list"})
    tags = [_node_text(n) for n in tree.css(".article__tag, .article-tags a, a[href*='/tag/']")]
    tags = list(dict.fromkeys(t for t in tags if t))
    cover = _meta(tree, "og:image") or None
    if cover:
        cover = _absolute(cover, canonical or url or BASE_URL)
    description = _meta(tree, "og:description") or _meta(tree, "description") or None
    original_source = None
    reprint = _node_text(tree.css_first(".article__reprinted-explain"))
    if reprint and ("来源于网络" in reprint or "原文链接" in reprint):
        original_source = "网络来源（页面未给出媒体名）"
    return HuxiuExtractionResult(
        title=title or None,
        subtitle=subtitle,
        authors=authors,
        published_at=published,
        canonical_url=canonical or url,
        section=section,
        tags=tags,
        description=description,
        body_html=body_html,
        body_text=body_text,
        blocks=blocks,
        images=images,
        cover_image=cover,
        original_source=original_source,
        metadata={"section": section, "tags": tags, "original_source": original_source, "blocks": blocks, "embedded_tag_data_present": "tag_name" in html},
    )


def parse_channel_page(html: str, *, base_url: str = BASE_URL) -> list[DiscoveredItem]:
    """Parse SSR article cards from ``/article/`` in display order."""
    tree = HTMLParser(html)
    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    for card in tree.css(".article-item-wrap"):
        anchor = next((a for a in card.css("a[href]") if ARTICLE_URL_RE.match(urljoin(base_url, a.attributes.get("href") or ""))), None)
        if anchor is None:
            continue
        url = normalize_huxiu_url(urljoin(base_url, anchor.attributes.get("href") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        title_node = card.css_first(".channel-title, .content-title")
        author_node = card.css_first(".author-name")
        image = card.css_first("img[src], img[data-src]")
        items.append(DiscoveredItem(url=url, title=_node_text(title_node), authors=[_node_text(author_node)] if _node_text(author_node) else [], image=_absolute((image.attributes.get("src") or image.attributes.get("data-src") or ""), url) if image else None))
    return items


class HuxiuAdapter(SourceAdapter):
    """Huxiu prototype adapter; not registered in the production site list yet."""

    def discover(self, *, fetcher: BaseFetcher, max_items: int, existing_urls: set[str] | None = None) -> list[DiscoveredItem]:
        html = fetcher.fetch(BASE_URL + "article/")
        existing = {normalize_huxiu_url(u) for u in (existing_urls or set())}
        return [item for item in parse_channel_page(html) if item.url not in existing][:max_items]

    def extract_article(self, article: Article, html: str, url: str = "") -> bool:
        result = extract_huxiu_article(html, url=url or article.canonical_url)
        if not result.title or len(result.body_text) < 80:
            return False
        mapped = result.to_article(source_id=article.source_id, source_name=article.source_name, language=article.language or "zh-CN")
        article.title = mapped.title
        article.authors = mapped.authors
        article.published_at = mapped.published_at or article.published_at
        article.canonical_url = mapped.canonical_url
        article.body_text = mapped.body_text
        article.body_html = mapped.body_html
        article.images = mapped.images
        article.lead_image = mapped.lead_image
        article.language = mapped.language
        return True
