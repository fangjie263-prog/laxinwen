"""Measure live Huxiu cursor discovery at several explicit limits."""

from __future__ import annotations

import json
import logging

from news.fetch import FetcherOptions, HttpxFetcher
from news.sources.huxiu import HuxiuAdapter


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    fetcher = HttpxFetcher(FetcherOptions(timeout=30, retries=2, min_interval=1, max_interval=1))
    try:
        rows = []
        for limit in (20, 50, 100):
            adapter = HuxiuAdapter("huxiu", "虎嗅")
            items = adapter.discover(fetcher=fetcher, max_items=limit, existing_urls=set())
            urls = [item.url for item in items]
            rows.append(
                {
                    "limit": limit,
                    "discovered": len(items),
                    "unique_urls": len(set(urls)),
                    "first_url": urls[0] if urls else None,
                    "last_url": urls[-1] if urls else None,
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    finally:
        fetcher.close()


if __name__ == "__main__":
    main()
