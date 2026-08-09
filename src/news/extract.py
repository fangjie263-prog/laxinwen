"""正文提取层 —— 基于 Trafilatura。

HTML → Trafilatura → title / author / date / text / images。

如果某站点 Trafilatura 效果不好：
1. 先调整参数（见站点配置 extract）
2. 再考虑站点级提取配置
3. 最后才考虑 Playwright（本项目第一版不默认启用）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import trafilatura

from .model import Article

logger = logging.getLogger(__name__)

# Trafilatura 的 date 解析依赖 dateparser，这里兜底用简单解析
_KNOWN_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
)


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    from datetime import datetime as _dt

    s = value.strip()
    for fmt in _KNOWN_FORMATS:
        try:
            dt = _dt.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return trafilatura.utils.sanitize_date(value)
    except Exception:
        return None


@dataclass
class ExtractedArticle:
    """Trafilatura 提取结果。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    text: str = ""
    images: list[str] = field(default_factory=list)
    lead_image: Optional[str] = None
    canonical_url: str = ""
    language: str = ""


def _extract_meta_datetime(html: str) -> Optional[datetime]:
    """从 HTML 的 meta 标签提取发布时间（article:published_time 优先）。"""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for prop in ("article:published_time", "datePublished", "pubdate"):
            for node in tree.css("meta"):
                if (node.attributes.get("property") or "").lower() == prop or \
                   (node.attributes.get("name") or "").lower() == prop:
                    content = node.attributes.get("content") or ""
                    dt = _parse_date(content)
                    if dt is not None:
                        return dt
    except Exception:
        pass
    return None


DEFAULT_CLEAN_PATTERNS: list[str] = [
    # 常见推广/订阅杂讯（站点级可通过配置覆盖）
    r"Escolha o ECO como fonte preferida no Google",
]


def _apply_clean_patterns(text: str, patterns: list[str]) -> str:
    """从正文中移除杂讯行（推广、订阅提示等）。"""
    if not text:
        return text
    import re

    result = text
    for pat in patterns:
        try:
            result = re.sub(pat, "", result, flags=re.DOTALL)
        except re.error:
            continue
    # 清理由此产生的多余空行
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def extract_article(html: str, *, url: str = "", site_extract: dict | None = None) -> ExtractedArticle:
    """从 HTML 提取文章正文。

    site_extract 可来自站点配置 ``extract``，用于覆盖 Trafilatura 参数。
    """
    extract_cfg = dict(site_extract or {})
    clean_patterns = extract_cfg.pop("clean_patterns", None)
    favor_recall = extract_cfg.pop("favor_recall", True) if "favor_recall" in extract_cfg else True
    output_format = extract_cfg.pop("output_format", "txt")
    url_override = extract_cfg.pop("url", None) or url
    # 明确 True/False 转换（Trafilatura 接受 bool）
    favor_recall = bool(favor_recall)

    result = trafilatura.extract(
        html,
        url=url_override,
        output_format=output_format,
        include_comments=False,
        include_tables=False,
        favor_recall=favor_recall,
        include_links=False,
        **extract_cfg,
    )
    try:
        metadata_obj = trafilatura.extract_metadata(html, default_url=url_override or None)
        meta: dict = metadata_obj.as_dict() if metadata_obj is not None else {}
    except Exception:
        meta = {}

    text = result if isinstance(result, str) else (result or "")

    authors: list[str] = []
    author_raw = meta.get("author")
    author_items: list[str] = []
    if isinstance(author_raw, str):
        author_items = [author_raw]
    elif isinstance(author_raw, (list, tuple)):
        author_items = [str(a) for a in author_raw]
    for raw in author_items:
        for name in str(raw).split(","):
            name = name.strip()
            if name and name not in authors:
                authors.append(name)

    images_raw = meta.get("image") or ""
    images: list[str] = []
    if isinstance(images_raw, str):
        if images_raw.strip():
            images = [images_raw.strip()]
    elif isinstance(images_raw, (list, tuple)):
        images = [str(i) for i in images_raw if str(i).strip()]
    lead_image = images[0] if images else None

    title = (meta.get("title") or "").strip()
    if not title:
        try:
            bare = trafilatura.bare_extraction(
                html, url=url_override, include_comments=False, favor_recall=favor_recall
            )
            if bare is not None:
                title = (bare.title or "").strip()
        except Exception:
            pass

    published = _parse_date(meta.get("date") or meta.get("date_modified"))
    if published is None or (published.hour == 0 and published.minute == 0):
        # 尝试从 HTML 的 article:published_time / datePublished meta 补全精确时间
        page_time = _extract_meta_datetime(html)
        if page_time is not None:
            published = page_time
    language = (meta.get("language") or "").strip()
    canonical = (meta.get("url") or url_override or "").strip()

    # 站点级正文杂讯清理
    if clean_patterns:
        text = _apply_clean_patterns(text, clean_patterns)

    return ExtractedArticle(
        title=title,
        authors=authors,
        published_at=published,
        text=text.strip(),
        images=images,
        lead_image=lead_image,
        canonical_url=canonical,
        language=language,
    )


def apply_extraction_to_article(article: Article, html: str, site_extract: dict | None = None) -> Article:
    """用 Trafilatura 提取结果更新 Article 的正文相关字段。"""
    extracted = extract_article(html, url=article.canonical_url, site_extract=site_extract)
    if extracted.title:
        article.title = extracted.title
    if extracted.authors:
        article.authors = extracted.authors
    if extracted.published_at:
        article.published_at = extracted.published_at
    if extracted.text:
        article.body_text = extracted.text
    if extracted.images:
        article.images = extracted.images
    if extracted.lead_image:
        article.lead_image = extracted.lead_image
    if extracted.canonical_url:
        article.canonical_url = extracted.canonical_url
    if extracted.language:
        article.language = extracted.language
    return article
