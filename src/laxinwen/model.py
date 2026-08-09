"""Unified Article data model — the contract between the scraping layer and a
future AI enrichment layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Return the current time as an aware, UTC datetime (ISO 8601 ready)."""
    return datetime.now(timezone.utc)


@dataclass
class Article:
    """A single normalized news article.

    ``canonical_url`` is used for URL-level deduplication and must be unique.
    ``published_at`` is stored in UTC as an ISO 8601 string by the storage
    layer; the in-memory value is a ``datetime`` (aware, UTC).
    """

    source_id: str
    source_name: str
    canonical_url: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    discovered_at: datetime = field(default_factory=utcnow)
    fetched_at: datetime | None = None
    body_text: str = ""
    body_html: str | None = None
    images: list[str] = field(default_factory=list)
    lead_image: str | None = None
    language: str | None = None
    status: str = "new"
    errors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str | None:
        """Primary key assigned by the storage layer (lastrowid)."""
        return getattr(self, "_id", None)

    @id.setter
    def id(self, value: int | str | None) -> None:
        self._id = value

    def published_at_iso(self) -> str | None:
        """ISO 8601 (UTC) representation of ``published_at``."""
        if self.published_at is None:
            return None
        dt = self.published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict (used by JSONL export and tests)."""
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "authors": list(self.authors),
            "published_at": self.published_at_iso(),
            "discovered_at": self.discovered_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "body_text": self.body_text,
            "body_html": self.body_html,
            "images": list(self.images),
            "lead_image": self.lead_image,
            "language": self.language,
            "status": self.status,
            "errors": list(self.errors),
        }
