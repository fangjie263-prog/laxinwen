"""The end-to-end pipeline: discover → fetch → extract → dedupe → store."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import SiteConfig
from .discover import Discoverer
from .extract import extract_article
from .fetch import Fetcher
from .model import Article, utcnow
from .normalize import canonicalize_url
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class FetchReport:
    site_id: str
    discovered: int = 0
    downloaded: int = 0
    extracted_ok: int = 0
    inserted: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_pipeline(
    site: SiteConfig,
    storage: Storage,
    *,
    fetcher: Fetcher | None = None,
    max_items: int = 50,
    max_download: int | None = None,
    limit: int = 0,
) -> FetchReport:
    """Run the full pipeline for one site. Never raises on per-article errors."""
    report = FetchReport(site_id=site.id)
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=site.request_interval)

    try:
        discoverer = Discoverer(fetcher)
        discovered = discoverer.discover(site, max_items=max_items)
        report.discovered = len(discovered)

        if max_download is not None:
            discovered = discovered[:max_download]

        for item in discovered:
            url = canonicalize_url(item.url)
            if storage.get_by_url(url) is not None:
                report.duplicates += 1
                continue

            article = Article(
                source_id=site.id,
                source_name=site.name,
                canonical_url=url,
                title=item.title,
                authors=item.authors,
                discovered_at=utcnow(),
            )
            if item.published_at:
                try:
                    article.published_at = datetime.fromisoformat(
                        item.published_at.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except ValueError:
                    pass

            try:
                html = fetcher.fetch_text(url)
                article.fetched_at = utcnow()
                report.downloaded += 1
            except Exception as exc:  # noqa: BLE001
                report.failed += 1
                report.errors.append(f"{url}: download failed: {exc}")
                article.status = "error"
                article.errors.append(f"download: {exc}")
                storage.insert(article, strip_site_suffix=site.title_strip_suffix)
                continue

            try:
                extracted = extract_article(
                    site.id,
                    site.name,
                    url,
                    html,
                    site_extract=site.extract,
                )
                article.title = extracted.title or article.title
                article.authors = extracted.authors or article.authors
                if extracted.published_at:
                    article.published_at = extracted.published_at
                article.body_text = extracted.body_text
                article.body_html = extracted.body_html
                article.images = extracted.images
                article.lead_image = extracted.lead_image
                article.language = extracted.language
                article.status = extracted.status
                article.errors = extracted.errors
                if extracted.status == "ok":
                    report.extracted_ok += 1
                else:
                    report.failed += 1
                    report.errors.append(f"{url}: extraction produced empty body")
            except Exception as exc:  # noqa: BLE001
                report.failed += 1
                report.errors.append(f"{url}: extraction failed: {exc}")
                article.status = "error"
                article.errors.append(f"extraction: {exc}")

            inserted = storage.insert(article, strip_site_suffix=site.title_strip_suffix)
            if inserted:
                report.inserted += 1
            else:
                report.duplicates += 1

            if limit and report.downloaded >= limit:
                break
    finally:
        if own_fetcher:
            fetcher.close()

    return report
