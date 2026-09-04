"""Bridge the completed NYT Chinese parser into the shared source pipeline."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..discover import DiscoveredItem, discover_from_rss
from ..fetch import BaseFetcher
from ..model import Article
from ..normalize import canonicalize_url
from ..nytcn import parse_nyt_article
from .base import SourceAdapter

logger = logging.getLogger(__name__)

_ARTICLE_URL_RE = re.compile(
    r"https?://cn\.nytimes\.com/[^?#]+/20\d{6}/[^?#]+/?$"
)
_DATE_RE = re.compile(r"/(20\d{6})/")


class NytChineseAdapter(SourceAdapter):
    """Official RSS discovery and the existing ``news.nytcn`` HTML parser."""

    def __init__(self, source_id: str, source_name: str, *, rss_url: str,
                 sections: list[dict] | None = None,
                 allow_summary_as_content: bool = False) -> None:
        super().__init__(source_id, source_name)
        self.rss_url = rss_url
        self.sections = list(sections or [])
        self.allow_summary_as_content = allow_summary_as_content

    @staticmethod
    def _section_items(html: str, section: dict) -> list[DiscoveredItem]:
        selector = section.get("link_selector", 'a[href*="/20"]')
        pattern = re.compile(section["article_url_pattern"]) if section.get("article_url_pattern") else _ARTICLE_URL_RE
        tree = HTMLParser(html)
        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        for node in tree.css(selector):
            href = node.attributes.get("href") or ""
            if not href:
                continue
            url = canonicalize_url(urljoin(section["url"], href))
            if not url or not pattern.search(url) or url in seen:
                continue
            seen.add(url)
            title = (node.attributes.get("title") or node.text(strip=True) or "").strip()
            published = None
            match = _DATE_RE.search(url)
            if match:
                published = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
            items.append(DiscoveredItem(url=url, title=title, published_at=published))
        return items

    def discover(self, *, fetcher: BaseFetcher, max_items: int,
                 existing_urls: set[str] | None = None) -> list[DiscoveredItem]:
        seen = {canonicalize_url(url) for url in (existing_urls or set())}
        result: list[DiscoveredItem] = []
        for item in discover_from_rss(self.rss_url, fetcher=fetcher):
            canonical = canonicalize_url(item.url)
            if not canonical or canonical in seen:
                continue
            if not self.allow_summary_as_content:
                item.content_html = None
            seen.add(canonical)
            result.append(item)
        # RSS 已足够满足小 limit 时无需额外请求栏目页；只有候选不足时才
        # 扫描配置中的栏目，避免 ``--limit 3`` 也请求十几个 section 页面。
        if len(result) < max_items:
            for section in self.sections:
                url = section.get("url")
                if not url:
                    continue
                try:
                    html = fetcher.fetch(url)
                    section_items = self._section_items(html, section)
                except Exception as exc:
                    logger.warning("[NYT] section discovery failed %s: %s", url, exc)
                    continue
                for item in section_items:
                    canonical = canonicalize_url(item.url)
                    if not canonical or canonical in seen:
                        continue
                    seen.add(canonical)
                    result.append(item)

        result.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return result[:max_items]

    def extract_article(self, article: Article, html: str, url: str = "") -> bool:
        parsed = parse_nyt_article(html, url=url or article.canonical_url)
        meta = parsed.metadata
        if not meta.title or not parsed.body_text:
            return False
        article.title = meta.title
        article.authors = list(meta.authors)
        article.published_at = meta.published_at or article.published_at
        article.canonical_url = meta.canonical_url or article.canonical_url
        article.body_text = parsed.body_text
        article.body_html = parsed.body_html or None
        article.images = [image["url"] for image in meta.images if image.get("url")]
        article.lead_image = meta.lead_image or (article.images[0] if article.images else None)
        article.language = meta.language or article.language
        return True
