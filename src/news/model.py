"""统一 Article 数据模型 —— 抓取层与未来 AI 层之间的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间（ISO 8601）。"""
    return datetime.now(timezone.utc)


@dataclass
class Article:
    """一篇新闻文章的统一表示。

    所有新闻来源最终都必须转换成该模型。
    其中 ``canonical_url`` 用于 URL 去重，
    ``published_at`` 统一为带 UTC 时区的 ISO 8601 时间。
    """

    source_id: str
    source_name: str
    canonical_url: str
    title: str
    body_text: str = ""
    body_html: Optional[str] = None
    authors: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    discovered_at: datetime = field(default_factory=utcnow)
    fetched_at: Optional[datetime] = None
    images: list[str] = field(default_factory=list)
    lead_image: Optional[str] = None
    language: str = ""
    status: str = "new"  # new / fetched / failed
    id: Optional[int] = None  # 数据库主键，入库后回填

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的 dict（时间转为 ISO 8601 字符串）。"""
        def _iso(dt: Optional[datetime]) -> Optional[str]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()

        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "authors": list(self.authors),
            "published_at": _iso(self.published_at),
            "discovered_at": _iso(self.discovered_at),
            "fetched_at": _iso(self.fetched_at),
            "body_text": self.body_text,
            "body_html": self.body_html,
            "images": list(self.images),
            "lead_image": self.lead_image,
            "language": self.language,
            "status": self.status,
        }
