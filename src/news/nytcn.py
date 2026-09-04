"""NYT Chinese (纽约时报中文网) source adapter.

Parses NYT Chinese article HTML pages and maps them into the unified Laxinwen
``Article`` model.  This adapter is data-driven rather than relying on
hard-coded CSS selectors for every section template, because the NYT Chinese
page layout wraps the article in a wide ``article-area`` container that also
holds navigation, related-article links, "most popular" widgets, and footer
chrome.

The extractor therefore:

1. reads page-level metadata from ``<meta>`` / JSON-LD / visible DOM,
2. locates the *real* body container by scoring candidate containers
   inside ``article`` / ``main`` / the page body,
3. walks that container in DOM order to build ``body_text`` / ``body_html``
   (paragraphs, headings, figures, images, lists, blockquotes, tables, video),
4. returns a metadata block + ordered blocks that callers can feed into
   ``news.model.Article``.

No login / CAPTCHA / paywall bypass is attempted; only public content is parsed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from selectolax.parser import HTMLParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SOURCE_ID = "nytchinese"
SOURCE_NAME = "NYT 纽约时报中文网"

# 相关报道 / 最受欢迎 等非正文区域 heading 关键词（识别并排除）
_NON_BODY_HEADING_KEYWORDS = (
    "相关报道",
    "最受欢迎",
    "recommended",
    "related",
    "most popular",
    "popular",
    "read more",
    "read next",
    "continue reading",
    "trending",
    "most read",
    "editors' picks",
    "最新报道",
    "猜你喜欢",
    "阅读最多",
)

# Navigation / utility sections class or id keywords that are never content.
_NON_BODY_CLASS_KEYWORDS = (
    "nav", "navigation", "menu", "header", "footer", "sidebar",
    "related", "popular", "recommended", "advert", "promo", "share",
    "subscribe", "newsletter", "app-promo", "most-popular",
    "recommendation", "reader-reactions", "comments", "language-switch",
    "section-heading", "breadcrumb",
)

# Heading tags in document-order priority for block extraction.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Block-level element tags that we keep as content blocks.
_BODY_BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "figure", "img", "figcaption", "ul", "ol", "li",
    "blockquote", "table", "video", "iframe", "pre",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class NytMetadata:
    """Extracted metadata for a NYT Chinese article."""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    section: str = ""
    canonical_url: str = ""
    description: str = ""
    language: str = "zh-CN"
    lead_image: str = ""
    images: list[dict] = field(default_factory=list)  # [{url, alt, caption}]


@dataclass
class ContentBlock:
    """A single content block in document order."""
    type: str  # paragraph | heading | figure | image | list | blockquote | table | video
    text: str = ""
    level: int = 0  # for heading types
    src: str = ""  # for image/figure/video
    alt: str = ""  # for image/figure
    caption: str = ""  # for figure
    items: list[str] = field(default_factory=list)  # for list

    def to_dict(self) -> dict:
        d = {"type": self.type}
        if self.text:
            d["text"] = self.text
        if self.level:
            d["level"] = self.level
        if self.src:
            d["src"] = self.src
        if self.alt:
            d["alt"] = self.alt
        if self.caption:
            d["caption"] = self.caption
        if self.items:
            d["items"] = self.items
        return d


@dataclass
class NytParseResult:
    """Complete parse result for an NYT Chinese article page."""
    metadata: NytMetadata = field(default_factory=NytMetadata)
    blocks: list[ContentBlock] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""

    @property
    def has_content(self) -> bool:
        return len(self.blocks) > 0


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------

def _parse_datetime(value: str) -> Optional[datetime]:
    """Parse a datetime string into timezone-aware UTC datetime."""
    if not value:
        return None
    s = value.strip()
    # ISO 8601 with timezone offset
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Common formats without timezone
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# HTML parsing utilities
# ---------------------------------------------------------------------------

def _node_class(node) -> str:
    """Get the class attribute string of a selectolax node."""
    return node.attributes.get("class", "") or ""


def _node_id(node) -> str:
    """Get the id attribute of a selectolax node."""
    return node.attributes.get("id", "") or ""


def _node_attrs(node) -> dict:
    return dict(node.attributes)


def _is_non_body_node(node) -> bool:
    """Heuristic: does this node appear to be page furniture rather than content?"""
    # Tag-level: nav/footer/script/style are never body
    tag = (getattr(node, "tag", "") or "").lower()
    if tag in ("nav", "footer", "script", "style", "noscript"):
        return True

    cls = (_node_class(node) + " " + _node_id(node)).lower()
    # Match more carefully: check for word-boundary patterns or common exact
    # substrings that indicate nav / related / popular / footer elements.
    if tag in ("header", "aside"):
        # <header> and <aside> elements are typically not article body
        return True

    for kw in _NON_BODY_CLASS_KEYWORDS:
        if kw in ("header", "footer", "nav", "navigation", "menu"):
            # These keywords should not match legitimate article content
            # containers like "article-header" (metadata) but should match
            # page-header / site-footer / main-nav / related-nav / etc.
            continue
        if kw in cls:
            return True

    # Specific patterns for non-body containers in class/id
    non_body_patterns = (
        r"(?:^|[-_])(?:site|page|main|top|bottom)(?:[-_])?(?:header|footer|nav)",
        r"related[-_](?:articles?|news|links?|stories?)",
        r"(?:most[-_]?popular|popular[-_]?articles?)",
        r"(?:recommend(?:ed|ation|s)?|trending|editors[-_]?pick)",
        r"(?:language[-_]?switcher|translate|share[-_]?(?:buttons?|tools?))",
        r"(?:app[-_]?promo|newsletter|subscribe|signup)",
        r"(?:comments?|reader[-_]?reactions?|reaction)",
        r"(?:breadcrumb|cookie[-_]?banner|advert|promo)",
        r"(?:sidebar|side[-_]?bar|aside)",
    )
    import re as _re
    for pat in non_body_patterns:
        if _re.search(pat, cls, _re.IGNORECASE):
            return True

    # If it's a heading with known non-body text
    if node.tag in _HEADING_TAGS:
        txt = (node.text(strip=True) or "").lower()
        if any(kw in txt for kw in _NON_BODY_HEADING_KEYWORDS):
            return True
    # Check data attributes for testids that indicate nav/related widgets
    for k, v in node.attributes.items():
        kl = k.lower()
        vl = (v or "").lower()
        if kl in ("data-testid", "role", "aria-label"):
            if any(kw in vl for kw in _NON_BODY_CLASS_KEYWORDS):
                # Only exclude if data-testid clearly indicates nav/widget
                if any(term in vl for term in ("nav", "related", "popular",
                                                "recommend", "footer",
                                                "sidebar", "language", "share")):
                    return True
    return False


def _has_substantial_text(node) -> bool:
    """Check if node contains meaningful article text (not just links/menu)."""
    text = node.text(strip=True) or ""
    if len(text) < 100:
        return False
    # Ratio of link text to total text should not be too high
    links = node.css("a")
    if not links:
        return True
    link_text = sum(len(a.text(strip=True) or "") for a in links)
    if len(text) > 0:
        return link_text / len(text) < 0.5
    return True


def _element_text(node) -> str:
    """Get clean text of an element."""
    return (node.text(separator=" ", strip=True) or "").strip()


def _img_url(img) -> str:
    """Resolve best image URL from an img node."""
    for attr in ("src", "data-src", "data-original", "data-url"):
        url = (img.attributes.get(attr) or "").strip()
        if url and url.startswith(("http://", "https://", "//")):
            return url
    # From srcset if present
    srcset = img.attributes.get("srcset", "")
    if srcset:
        # Pick the highest resolution candidate
        candidates = []
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            pieces = part.split()
            if pieces:
                url = pieces[0]
                if url.startswith(("http://", "https://", "//")):
                    candidates.append(url)
        if candidates:
            return candidates[-1]  # last candidate is usually largest
    return ""


def _abs_url(url: str, base_url: str) -> str:
    """Convert a relative URL to absolute using base_url."""
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        from urllib.parse import urlsplit

        parts = urlsplit(base_url)
        return f"{parts.scheme}://{parts.netloc}{url}"
    return url


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_metadata(tree: HTMLParser, url: str) -> NytMetadata:
    """Extract all article metadata from the page."""
    meta = NytMetadata()
    meta.canonical_url = url

    # --- Collect meta tags ---
    meta_tags: dict[str, str] = {}
    for tag in tree.css("meta"):
        attrs = _node_attrs(tag)
        name = (attrs.get("name") or attrs.get("property") or "").lower()
        content = attrs.get("content", "")
        if name and content:
            meta_tags[name] = content

    # --- title: h1 > meta headline > og:title > HTML title ---
    h1 = tree.css_first("h1")
    if h1:
        meta.title = _element_text(h1)
    if not meta.title:
        for key in ("headline", "og:title", "twitter:title", "title"):
            if meta_tags.get(key):
                meta.title = meta_tags[key].strip()
                break
    if not meta.title:
        title_node = tree.css_first("title")
        if title_node:
            raw = _element_text(title_node)
            # Clean site suffix
            meta.title = re.sub(
                r"\s*[|\-–—]\s*(纽约时报中文网|NYT|The New York Times|纽约时报|cn\.nytimes\.com).*$",
                "", raw,
            ).strip()

    # --- published_at: article:published_time > ptime > date > JSON-LD > visible ---
    time_str = meta_tags.get("article:published_time") or \
        meta_tags.get("ptime") or meta_tags.get("pdate") or \
        meta_tags.get("date") or meta_tags.get("pubdate") or \
        meta_tags.get("datePublished")
    if time_str:
        meta.published_at = _parse_datetime(time_str)
    if not meta.published_at:
        # Try JSON-LD
        for ld in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(ld.text() or "")
                if isinstance(data, dict):
                    dp = data.get("datePublished") or data.get("dateModified")
                    if dp:
                        meta.published_at = _parse_datetime(str(dp))
                        break
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            dp = item.get("datePublished") or item.get("dateModified")
                            if dp:
                                meta.published_at = _parse_datetime(str(dp))
                                break
                    if meta.published_at:
                        break
            except json.JSONDecodeError:
                continue
    if not meta.published_at:
        # Look for <time datetime> in article area
        time_el = tree.css_first("time[datetime]")
        if time_el:
            dt_attr = time_el.attributes.get("datetime", "")
            meta.published_at = _parse_datetime(dt_attr)

    # --- authors: meta byline > JSON-LD author > visible byline ---
    author_str = meta_tags.get("byline") or meta_tags.get("byl") or \
        meta_tags.get("author") or meta_tags.get("parsely-author")
    if author_str:
        parts = re.split(r"[;,，、]+", author_str)
        meta.authors = [p.strip() for p in parts if p.strip()]
    if not meta.authors:
        # JSON-LD
        for ld in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(ld.text() or "")
                candidates = []
                if isinstance(data, dict):
                    candidates.append(data)
                elif isinstance(data, list):
                    candidates.extend(data)
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    auth = item.get("author")
                    if auth:
                        if isinstance(auth, list):
                            for a in auth:
                                if isinstance(a, dict):
                                    name = a.get("name", "")
                                    if name:
                                        meta.authors.append(str(name))
                                elif isinstance(a, str):
                                    meta.authors.append(a)
                        elif isinstance(auth, dict):
                            name = auth.get("name", "")
                            if name:
                                meta.authors.append(str(name))
                        elif isinstance(auth, str):
                            meta.authors.append(auth)
                        if meta.authors:
                            break
            except json.JSONDecodeError:
                continue
    if not meta.authors:
        # Visible byline from DOM: look for common byline patterns
        for sel in (
            'div[class*="byline" i]', 'span[class*="byline" i]',
            'p[class*="byline" i]', 'div[class*="author" i]',
            'span[class*="author" i]',
        ):
            try:
                node = tree.css_first(sel)
                if node:
                    txt = _element_text(node)
                    if txt and len(txt) < 300 and not any(
                        kw in txt for kw in ("关注", "新浪", "腾讯", "订阅")
                    ):
                        # Split by 文/ or , or ·
                        cleaned = re.sub(r"^(文|作者)[:：]?\s*", "", txt)
                        parts = re.split(r"[;；,，、]", cleaned)
                        meta.authors = [p.strip() for p in parts if p.strip()]
                        if meta.authors:
                            break
            except Exception:
                continue

    # --- section: meta section > URL path ---
    meta.section = meta_tags.get("article:section") or meta_tags.get("section") or ""
    if not meta.section:
        m = re.search(r"cn\.nytimes\.com/([^/]+)/", url)
        if m:
            meta.section = m.group(1)
    # Normalize section name to lowercase slug
    meta.section = meta.section.strip().lower()

    # --- canonical URL ---
    link = tree.css_first('link[rel="canonical"]')
    if link:
        href = link.attributes.get("href", "")
        if href:
            meta.canonical_url = href

    # --- description ---
    meta.description = meta_tags.get("description") or meta_tags.get("og:description", "")

    # --- lead image ---
    og_img = meta_tags.get("og:image")
    if og_img:
        meta.lead_image = og_img
    if not meta.lead_image:
        fig = tree.css_first("article figure img, figure img, article img")
        if fig:
            meta.lead_image = _abs_url(_img_url(fig), url)

    # --- images list ---
    images: list[dict] = []
    seen_urls: set[str] = set()
    for fig in tree.css("figure"):
        img = fig.css_first("img")
        if not img:
            continue
        src = _abs_url(_img_url(img), url)
        if not src or src in seen_urls:
            continue
        seen_urls.add(src)
        cap_node = fig.css_first("figcaption")
        caption = _element_text(cap_node) if cap_node else ""
        images.append({
            "url": src,
            "alt": img.attributes.get("alt", ""),
            "caption": caption,
        })
    # Fallback: standalone img in article
    if not images:
        article = tree.css_first("article")
        scope = article if article else tree.body
        if scope:
            for img in scope.css("img"):
                src = _abs_url(_img_url(img), url)
                if src and src not in seen_urls:
                    seen_urls.add(src)
                    images.append({
                        "url": src,
                        "alt": img.attributes.get("alt", ""),
                        "caption": "",
                    })
    meta.images = images

    return meta


# ---------------------------------------------------------------------------
# Body container detection
# ---------------------------------------------------------------------------

def _score_container(node) -> float:
    """Score a candidate body container based on its content profile."""
    text = (node.text(strip=True) or "")
    if len(text) < 200:
        return -100

    score = 0.0
    p_count = len(node.css("p"))
    img_count = len(node.css("img"))
    fig_count = len(node.css("figure"))
    cap_count = len(node.css("figcaption"))
    heading_count = len(node.css("h1,h2,h3,h4,h5,h6"))
    link_count = len(node.css("a"))

    # Prefer containers with multiple paragraphs
    score += min(p_count, 40) * 3.0
    # Text volume: ideal article body is 1000-30000 chars
    if 1000 <= len(text) <= 30000:
        score += 25
    elif len(text) < 600:
        score += 5
    # Images and figures are content signals
    score += min(img_count, 8) * 1.5
    score += min(fig_count, 6) * 2.0
    score += min(cap_count, 5) * 1.0
    # Headings add value
    score += min(heading_count, 8) * 0.5
    # Too many links for the text volume suggests navigation/list
    if len(text) > 0:
        link_ratio = link_count / max(p_count, 1)
        if p_count == 0 and link_ratio > 3:
            score -= 30
    # Prefer article/main/section tags
    tag = getattr(node, "tag", "")
    if tag == "article":
        score += 20
    elif tag == "main":
        score += 15
    elif tag == "section":
        score += 10
    # Penalize containers with too much padding text (full-page wrapper)
    if len(text) > 60000:
        score -= 40
    # Penalize if node contains mostly navigation/related content
    if _is_non_body_node(node):
        score -= 50

    return score


def _find_body_container(tree: HTMLParser) -> Optional[object]:
    """Find the most likely article body container in the page."""
    body = tree.body
    if not body:
        return None

    best_node = None
    best_score = float("-inf")

    # First try semantic containers
    for selector in ("article", "main"):
        node = tree.css_first(selector)
        if node:
            s = _score_container(node)
            if s > best_score:
                best_score = s
                best_node = node

    # Then scan generic containers inside article/main if found,
    # or scan the whole body
    scope = tree.css_first("article") or tree.css_first("main") or body

    # Check child containers that might be the real body
    for node in scope.css("section, div, article"):
        # Skip containers that are obviously not body
        if node is scope:
            continue
        # Skip if parent is already a good candidate
        s = _score_container(node)
        # Also consider whether the node's text is mostly covered by its children
        node_text = (node.text(strip=True) or "")
        if not node_text:
            continue
        s = _score_container(node)
        if s > best_score:
            best_score = s
            best_node = node

    # If nothing found with good score, fall back to article/main
    if best_score < 0:
        for selector in ("article", "main"):
            node = tree.css_first(selector)
            if node:
                return node

    return best_node


def _collect_text_blocks(node, blocks: list[ContentBlock], *, max_depth: int = 10, base_url: str = "") -> None:
    """Walk a DOM subtree in order and collect content blocks."""
    if max_depth <= 0:
        return

    tag = node.tag.lower() if hasattr(node, "tag") else ""

    # Skip script/style/nav elements entirely
    if tag in ("script", "style", "nav", "noscript"):
        return

    # If this is a non-body container (related articles, footer, etc), skip
    if _is_non_body_node(node):
        return

    # Process by tag type -- each handler RETURNS so we don't double-process children
    if tag in ("p", "blockquote", "pre"):
        txt = _element_text(node)
        if txt and len(txt) >= 2:
            btype = "blockquote" if tag == "blockquote" else ("pre" if tag == "pre" else "paragraph")
            blocks.append(ContentBlock(type=btype, text=txt))
        return

    if tag in _HEADING_TAGS:
        txt = _element_text(node)
        if txt:
            level = int(tag[1])
            # Skip title h1 (it's in metadata, not body blocks) if it's the main heading
            if tag == "h1":
                return
            blocks.append(ContentBlock(type="heading", text=txt, level=level))
        return

    if tag == "img":
        src = _abs_url(_img_url(node), base_url)
        alt = node.attributes.get("alt", "")
        if src:
            blocks.append(ContentBlock(type="image", src=src, alt=alt))
        return

    if tag == "figure":
        # Extract figure with image and caption
        img = node.css_first("img")
        src = ""
        alt = ""
        if img:
            src = _abs_url(_img_url(img), base_url)
            alt = img.attributes.get("alt", "")
        cap_node = node.css_first("figcaption")
        caption = _element_text(cap_node) if cap_node else ""
        if src:
            blocks.append(ContentBlock(
                type="figure", src=src, alt=alt, caption=caption
            ))
        elif caption:
            # Figure without image but with caption - keep caption
            blocks.append(ContentBlock(type="figure", caption=caption))
        return

    if tag == "figcaption":
        # Already captured within figure handling
        return

    if tag in ("ul", "ol"):
        items = []
        for li in node.css(":scope > li"):
            txt = _element_text(li)
            if txt:
                items.append(txt)
        # Fallback: direct children if :scope not supported
        if not items:
            for child in node.iter(include_text=False):
                if hasattr(child, "tag") and child.tag == "li":
                    txt = _element_text(child)
                    if txt:
                        items.append(txt)
        if items:
            blocks.append(ContentBlock(type="list", items=items))
        return

    if tag == "li":
        # If directly encountered (not in list), treat as list item
        txt = _element_text(node)
        if txt:
            blocks.append(ContentBlock(type="list", items=[txt]))
        return

    if tag == "table":
        # Serialize table content
        rows = []
        for tr in node.css("tr"):
            cells = [_element_text(td) for td in tr.css("td,th")]
            rows.append(" | ".join(cells))
        if rows:
            blocks.append(ContentBlock(type="table", text="\n".join(rows)))
        return

    if tag == "video":
        src = ""
        for src_attr in ("src", "data-src"):
            val = node.attributes.get(src_attr, "")
            if val:
                src = val
                break
        if not src:
            source_el = node.css_first("source")
            if source_el:
                src = source_el.attributes.get("src", "")
        if src:
            blocks.append(ContentBlock(type="video", src=src))
        return

    # For other containers (div, section, article, main, span, etc.),
    # recursively process child elements in document order.
    # But skip if this node is page furniture.
    for child in node.iter():
        if not hasattr(child, "tag"):
            continue
        if child is node:
            continue
        _collect_text_blocks(child, blocks, max_depth=max_depth - 1, base_url=base_url)



# ---------------------------------------------------------------------------
# Block to body_text / body_html conversion
# ---------------------------------------------------------------------------

def _block_to_text(block: ContentBlock) -> str:
    """Convert a block to plain text."""
    if block.type == "paragraph":
        return block.text
    if block.type == "heading":
        return block.text
    if block.type == "blockquote":
        return f"“{block.text}”"
    if block.type == "list":
        return "\n".join(f"- {item}" for item in block.items)
    if block.type == "figure":
        parts = []
        if block.alt:
            parts.append(f"[图片: {block.alt}]")
        elif block.src:
            parts.append("[图片]")
        if block.caption:
            parts.append(block.caption)
        return " ".join(parts)
    if block.type == "image":
        return f"[图片: {block.alt or '无说明'}]" if block.alt else "[图片]"
    if block.type == "table":
        return block.text
    if block.type == "video":
        return "[视频]"
    return block.text


def _block_to_html(block: ContentBlock) -> str:
    """Convert a block to semantic HTML."""
    def esc(s: str) -> str:
        return escape(s or "")

    if block.type == "paragraph":
        return f"<p>{esc(block.text)}</p>"
    if block.type == "heading":
        return f"<h{block.level}>{esc(block.text)}</h{block.level}>"
    if block.type == "blockquote":
        return f"<blockquote><p>{esc(block.text)}</p></blockquote>"
    if block.type == "list":
        items = "".join(f"<li>{esc(item)}</li>" for item in block.items)
        return f"<ul>{items}</ul>"
    if block.type == "figure":
        src = esc(block.src)
        alt = esc(block.alt)
        parts = []
        parts.append(f'<figure class="content-image">')
        parts.append(f'<img src="{src}" alt="{alt}"')
        if block.alt:
            parts[-1] += f' data-alt="{alt}"'
        parts[-1] += ">"
        if block.caption:
            parts.append(f"<figcaption>{esc(block.caption)}</figcaption>")
        parts.append("</figure>")
        return "".join(parts)
    if block.type == "image":
        return f'<figure class="content-image"><img src="{esc(block.src)}" alt="{esc(block.alt)}"></figure>'
    if block.type == "table":
        # Minimal serialization as rows
        lines = block.text.split("\n")
        rows = []
        for line in lines:
            cells = line.split(" | ")
            rows.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in cells) + "</tr>")
        return f"<table>{''.join(rows)}</table>"
    if block.type == "video":
        return f'<video src="{esc(block.src)}" controls></video>'
    return ""


# ---------------------------------------------------------------------------
# Main parse entry point
# ---------------------------------------------------------------------------

def parse_nyt_article(html: str, *, url: str = "") -> NytParseResult:
    """Parse a NYT Chinese article HTML page.

    Args:
        html: Raw HTML of the NYT Chinese article page.
        url: The canonical URL of the article (used for image resolution and section fallback).

    Returns:
        NytParseResult with metadata, ordered blocks, body_text and body_html.
    """
    result = NytParseResult()

    try:
        tree = HTMLParser(html)
    except Exception as exc:
        logger.error("HTML parse error: %s", exc)
        return result

    # 1. Extract metadata
    result.metadata = _extract_metadata(tree, url)

    # 2. Find and extract body content
    body_container = _find_body_container(tree)
    if body_container is not None:
        _collect_text_blocks(body_container, result.blocks, base_url=url)

        # Filter out very short/insignificant blocks if body is overwhelmingly nav
        if len(result.blocks) < 3:
            # Try broader approach: walk the article container directly
            logger.debug("Few blocks found, retrying with article container")
            result.blocks = []
            article = tree.css_first("article") or tree.css_first("main")
            if article:
                _collect_text_blocks(article, result.blocks, base_url=url)

    # 3. Build body_text and body_html
    text_parts = []
    html_parts = []
    for block in result.blocks:
        bt = _block_to_text(block)
        bh = _block_to_html(block)
        if bt and bt.strip():
            text_parts.append(bt.strip())
        if bh:
            html_parts.append(bh)

    result.body_text = "\n\n".join(text_parts)
    result.body_html = "\n".join(html_parts)

    return result


# ---------------------------------------------------------------------------
# Convenience: convert to Laxinwen Article
# ---------------------------------------------------------------------------

def build_article(
    html: str,
    *,
    url: str,
    source_id: str = SOURCE_ID,
    source_name: str = SOURCE_NAME,
) -> dict:
    """Parse NYT article HTML and return data for building a news.model.Article.

    Returns a dict matching the keyword args of ``news.model.Article``.
    """
    parsed = parse_nyt_article(html, url=url)
    meta = parsed.metadata

    # Image list for Article.images (list[str] of URLs)
    image_urls = [img["url"] for img in meta.images if img.get("url")]
    lead_image = meta.lead_image or (image_urls[0] if image_urls else "")

    return {
        "source_id": source_id,
        "source_name": source_name,
        "canonical_url": meta.canonical_url or url,
        "title": meta.title,
        "authors": meta.authors,
        "published_at": meta.published_at,
        "body_text": parsed.body_text,
        "body_html": parsed.body_html,
        "images": image_urls,
        "lead_image": lead_image,
        "language": meta.language,
        "status": "fetched",
        # Metadata for tests/debug
        "section": meta.section,
        "description": meta.description,
        "blocks": [b.to_dict() for b in parsed.blocks],
    }
