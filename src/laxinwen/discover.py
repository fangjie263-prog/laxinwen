"""News discovery: RSS/Atom via feedparser, or HTML list pages via selectolax."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import feedparser
from selectolax.parser import HTMLParser

from .config import ListConfig, SiteConfig
from .fetch import Fetcher
from .normalize import canonicalize_url

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredItem:
    url: str
    title: str = ""
    published_at: str | None = None  # ISO 8601 string from feed
    authors: list[str] = field(default_factory=list)


class Discoverer:
    """Discovers article URLs for a site using RSS first, then HTML lists."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    def discover(self, site: SiteConfig, *, max_items: int = 50) -> list[DiscoveredItem]:
        seen: dict[str, DiscoveredItem] = {}

        # 1) official RSS / RSSHub
        for kind, url in site.effective_sources():
            if kind in ("rss", "rsshub"):
                try:
                    items = self._from_rss(url)
                    logger.info("RSS source %s yielded %d items", url, len(items))
                    for it in items:
                        seen.setdefault(canonicalize_url(it.url), it)
                except Exception as exc:  # noqa: BLE001 — one source failing must not stop the run
                    logger.warning("RSS source %s failed: %s", url, exc)
            elif kind == "html":
                try:
                    items = self._from_html(site, url)
                    logger.info("HTML list %s yielded %d items", url, len(items))
                    for it in items:
                        seen.setdefault(canonicalize_url(it.url), it)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("HTML list %s failed: %s", url, exc)

        result = list(seen.values())
        if max_items:
            result = result[:max_items]
        return result

    def _from_rss(self, url: str) -> list[DiscoveredItem]:
        text = self.fetcher.fetch_text(url)
        parsed = feedparser.parse(text)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"feedparser could not parse feed (bozo): {parsed.bozo_exception}")
        items: list[DiscoveredItem] = []
        for e in parsed.entries:
            link = e.get("link") or e.get("id") or ""
            if not link:
                continue
            published = None
            if e.get("published_parsed"):
                import time as _time

                published = _time.strftime("%Y-%m-%dT%H:%M:%SZ", e.published_parsed)
            authors = []
            for key in ("authors", "author_detail"):
                val = e.get(key)
                if isinstance(val, list):
                    authors.extend(a.get("name", "") for a in val if a.get("name"))
                elif isinstance(val, dict) and val.get("name"):
                    authors.append(val["name"])
            if e.get("author") and e["author"] not in authors:
                authors.append(e["author"])
            items.append(
                DiscoveredItem(
                    url=link,
                    title=e.get("title", "").strip(),
                    published_at=published,
                    authors=authors,
                )
            )
        return items

    def _from_html(self, site: SiteConfig, list_url: str) -> list[DiscoveredItem]:
        list_config = next((lc for lc in site.lists if lc.url == list_url), None)
        html = self.fetcher.fetch_text(list_url)
        tree = HTMLParser(html)
        items: list[DiscoveredItem] = []

        selector = list_config.link_selector if list_config else None
        if selector:
            anchors = tree.css(selector)
        else:
            anchors = tree.css("a[href]")

        pattern = list_config.article_url_pattern if list_config else None
        rx = re.compile(pattern) if pattern else None

        for a in anchors:
            # The selector may point at the <h3> inside the <a>; walk up to the
            # enclosing anchor to read its href.
            node = a
            while node is not None and node.tag != "a":
                node = node.parent
            if node is None:
                continue
            href = node.attributes.get("href", "")
            if rx and not rx.search(href):
                continue
            title = (a.text() or "").strip()
            items.append(DiscoveredItem(url=href, title=title))
        return items
