"""Bridge the completed NYT Chinese parser into the shared source pipeline."""

from __future__ import annotations

from ..discover import DiscoveredItem, discover_from_rss
from ..fetch import BaseFetcher
from ..model import Article
from ..normalize import canonicalize_url
from ..nytcn import parse_nyt_article
from .base import SourceAdapter


class NytChineseAdapter(SourceAdapter):
    """Official RSS discovery and the existing ``news.nytcn`` HTML parser."""

    def __init__(self, source_id: str, source_name: str, *, rss_url: str) -> None:
        super().__init__(source_id, source_name)
        self.rss_url = rss_url

    def discover(self, *, fetcher: BaseFetcher, max_items: int,
                 existing_urls: set[str] | None = None) -> list[DiscoveredItem]:
        seen = {canonicalize_url(url) for url in (existing_urls or set())}
        result: list[DiscoveredItem] = []
        for item in discover_from_rss(self.rss_url, fetcher=fetcher):
            canonical = canonicalize_url(item.url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            result.append(item)
            if len(result) >= max_items:
                break
        return result

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
