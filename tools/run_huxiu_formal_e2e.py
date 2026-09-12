"""Run a polite live Huxiu discovery -> Pipeline -> SQLite verification."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from news.fetch import FetcherOptions, HttpxFetcher
from news.pipeline import Pipeline
from news.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "huxiu-recon" / ".formal-e2e.sqlite3"
REPORT_PATH = ROOT / "huxiu-recon" / "formal-e2e.json"


def main() -> None:
    sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    DB_PATH.unlink(missing_ok=True)
    fetcher = HttpxFetcher(
        FetcherOptions(timeout=30, retries=2, min_interval=1, max_interval=1, article_interval=3)
    )
    try:
        with Storage(DB_PATH) as storage:
            pipeline = Pipeline(storage, fetcher=fetcher, max_items=3)
            stats = pipeline.run_site("huxiu")
            rows = []
            for article in storage.list_articles(source_id="huxiu", limit=3):
                rows.append(
                    {
                        "url": article.canonical_url,
                        "title": article.title,
                        "authors": article.authors,
                        "published_at": article.published_at.isoformat() if article.published_at else None,
                        "body_length": len(article.body_text),
                        "images": len(article.images),
                        "status": article.status,
                    }
                )
        result = {
            "discovered": stats.discovered,
            "deduplicated": stats.discovered - stats.skipped_dup,
            "selected": 3,
            "fetched_ok": stats.fetched_ok,
            "extracted_ok": stats.extracted_ok,
            "stored": len(rows),
            "failed": stats.failed,
            "articles": rows,
        }
        REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        fetcher.close()
        DB_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
