"""laxinwen — a personal financial news collection and research database.

The project composes mature open-source components (httpx, feedparser,
trafilatura, selectolax) into a simple pipeline:

    news site → discover URLs → download → extract → dedupe → SQLite → export
"""

__version__ = "0.1.0"
