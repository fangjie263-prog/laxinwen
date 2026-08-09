"""新闻发现机制。

优先级：
1. 网站官方 RSS / Atom（feedparser）
2. RSSHub（feedparser 解析同一格式）
3. 网站公开栏目页（selectolax 提取文章链接）
4. 站内搜索（第一阶段不实现）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import feedparser
from selectolax.parser import HTMLParser

from .fetch import BaseFetcher
from .model import Article
from .normalize import canonicalize_url

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredItem:
    """发现到的文章条目（未下载正文）。"""

    url: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    summary: str = ""
    content_html: Optional[str] = None
    image: Optional[str] = None

    def to_article(self, source_id: str, source_name: str, language: str = "") -> Article:
        return Article(
            source_id=source_id,
            source_name=source_name,
            canonical_url=canonicalize_url(self.url),
            title=(self.title or "").strip(),
            authors=self.authors,
            published_at=self.published_at,
            body_text="",
            body_html=self.content_html,
            lead_image=self.image,
            language=language,
            status="new",
        )


def _parse_datetime(value: Optional[str], struct: Optional[object] = None) -> Optional[datetime]:
    """将 feedparser 的日期字段解析为带 UTC 时区的 datetime。

    优先使用 feedparser 已解析好的 time.struct_time（published_parsed/updated_parsed），
    它带 UTC 语义；其次尝试宽松解析原始字符串。
    """
    if struct is not None:
        try:
            dt = datetime(*struct[:6], tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            pass
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        from dateutil import parser as dateparser

        dt = dateparser.parse(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


def discover_from_rss(feed_url: str, *, fetcher: BaseFetcher | None = None) -> list[DiscoveredItem]:
    """从 RSS / Atom 解析文章条目（官方 RSS 或 RSSHub 通用）。

    - 若传入 fetcher：用 httpx 下载（带超时/重试/UA/节流），feedparser 只做解析；
    - 否则：feedparser 直接解析（默认 urllib）。
    """
    if fetcher is not None:
        try:
            raw = fetcher.fetch(feed_url)
        except Exception as exc:
            raise ValueError(f"RSS 下载失败: {feed_url} ({exc})") from exc
        parsed = feedparser.parse(raw)
    else:
        parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败: {feed_url} ({getattr(parsed, 'bozo_exception', '')})")
    items: list[DiscoveredItem] = []
    for e in parsed.entries:
        link = e.get("link", "")
        if not link:
            continue
        authors = []
        if e.get("authors"):
            authors = [a.get("name", "") for a in e["authors"] if a.get("name")]
        elif e.get("author"):
            authors = [e["author"]]
        image = None
        media = e.get("media_content") or e.get("media_thumbnail")
        if media:
            image = media[0].get("url")
        if not image:
            encl = e.get("enclosures") or []
            for enc in encl:
                if enc.get("type", "").startswith("image/"):
                    image = enc.get("href")
                    break
        items.append(
            DiscoveredItem(
                url=link,
                title=e.get("title", "").strip(),
                authors=authors,
                published_at=_parse_datetime(
                    e.get("published") or e.get("updated"),
                    e.get("published_parsed") or e.get("updated_parsed"),
                ),
                summary=re.sub(r"<[^>]+>", "", e.get("summary", "")).strip(),
                content_html=e.get("content")[0].get("value") if e.get("content") else None,
                image=image,
            )
        )
    return items


def discover_from_list_page(
    list_url: str,
    *,
    fetcher: BaseFetcher,
    link_selector: str,
    article_url_pattern: str | None = None,
    max_items: int = 50,
) -> list[DiscoveredItem]:
    """从栏目页解析文章链接（selectolax）。

    - link_selector：文章链接的 CSS selector（由站点配置提供）
    - article_url_pattern：可选，文章 URL 正则；用于过滤非文章链接
    """
    html = fetcher.fetch(list_url)
    tree = HTMLParser(html)
    pattern = re.compile(article_url_pattern) if article_url_pattern else None

    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    for node in tree.css(link_selector):
        href = node.attributes.get("href") or node.attributes.get("data-href") or ""
        if not href:
            continue
        # 解析相对路径
        if href.startswith("/"):
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(list_url)
            href = urlunsplit((parts.scheme, parts.netloc, href, "", ""))
        canon = canonicalize_url(href)
        if not canon or canon in seen:
            continue
        if pattern and not pattern.search(href):
            continue
        seen.add(canon)
        items.append(DiscoveredItem(url=canon))
        if len(items) >= max_items:
            break
    return items


def discover_for_site(
    cfg: dict,
    *,
    fetcher: BaseFetcher,
    max_items: int = 50,
) -> list[DiscoveredItem]:
    """按站点配置执行发现流程（RSS → RSSHub → 栏目页）。"""
    source_id = cfg.get("id", "")
    source_name = cfg.get("name", "")

    # 1. 官方 RSS
    if cfg.get("rss"):
        logger.info("[%s] 尝试官方 RSS: %s", source_id, cfg["rss"])
        try:
            items = discover_from_rss(cfg["rss"], fetcher=fetcher)
            if items:
                logger.info("[%s] 官方 RSS 发现 %d 条", source_id, len(items))
                return items[:max_items]
            logger.warning("[%s] 官方 RSS 无条目，尝试下一个来源", source_id)
        except Exception as exc:
            logger.warning("[%s] 官方 RSS 失败: %s", source_id, exc)

    # 2. RSSHub
    if cfg.get("rsshub"):
        logger.info("[%s] 尝试 RSSHub: %s", source_id, cfg["rsshub"])
        try:
            items = discover_from_rss(cfg["rsshub"], fetcher=fetcher)
            if items:
                logger.info("[%s] RSSHub 发现 %d 条", source_id, len(items))
                return items[:max_items]
            logger.warning("[%s] RSSHub 无条目", source_id)
        except Exception as exc:
            logger.warning("[%s] RSSHub 失败: %s", source_id, exc)

    # 3. 栏目页
    lists = cfg.get("lists") or []
    for lst in lists:
        url = lst.get("url", "")
        if not url:
            continue
        logger.info("[%s] 尝试栏目页: %s", source_id, url)
        try:
            items = discover_from_list_page(
                url,
                fetcher=fetcher,
                link_selector=lst.get("link_selector", ""),
                article_url_pattern=lst.get("article_url_pattern") or cfg.get("article_url_pattern"),
                max_items=max_items,
            )
            if items:
                logger.info("[%s] 栏目页发现 %d 条", source_id, len(items))
                return items
        except Exception as exc:
            logger.warning("[%s] 栏目页失败: %s", source_id, exc)

    return []
