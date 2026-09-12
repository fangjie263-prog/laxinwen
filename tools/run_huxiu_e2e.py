"""Run a polite live Huxiu discovery -> fetch -> extract -> Article mapping."""

from __future__ import annotations

import json
import logging

from news.fetch import FetcherOptions, HttpxFetcher
from news.model import Article
from news.sources.huxiu import HuxiuAdapter, extract_huxiu_article


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fetcher = HttpxFetcher(FetcherOptions(timeout=30, retries=1, min_interval=0, max_interval=0))
    try:
        adapter = HuxiuAdapter("huxiu", "虎嗅")
        discovered = adapter.discover(fetcher=fetcher, max_items=5)
        rows = []
        fetched_ok = 0
        extracted_ok = 0
        for item in discovered[:3]:
            try:
                html = fetcher.fetch_article(item.url)
                fetched_ok += 1
                result = extract_huxiu_article(html, url=item.url)
                article = result.to_article()
                extracted_ok += int(bool(article.title and len(article.body_text) >= 80))
                rows.append({
                    "url": article.canonical_url,
                    "title": article.title,
                    "authors": article.authors,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                    "section": result.section,
                    "body_length": len(article.body_text),
                    "paragraphs": sum(block["type"] == "paragraph" for block in result.blocks),
                    "images": len(article.images),
                    "original_source": result.original_source,
                    "article_type": "Article-compatible",
                })
            except Exception as exc:  # pragma: no cover - live error reporting
                rows.append({"url": item.url, "error": repr(exc)})
        print(json.dumps({"discovered": len(discovered), "deduplicated": len({x.url for x in discovered}), "selected": 3, "fetched_ok": fetched_ok, "extracted_ok": extracted_ok, "failed": len(rows) - extracted_ok, "articles": rows}, ensure_ascii=False, indent=2))
    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
