"""HTTP fetching with polite defaults (UA, timeout, retry, rate limiting)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "laxinwen/0.1 (+personal news research; no commercial use)"
)

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2


class Fetcher:
    """HTTP fetcher abstraction.

    Future dynamic-rendering support (e.g. a PlaywrightFetcher) can subclass
    or replace this without touching the pipeline.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_UA,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        min_interval: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self._last_request_at = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
        )

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def fetch(self, url: str) -> httpx.Response:
        """GET ``url`` with retries; raise on persistent HTTP errors."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self._client.get(url)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * (attempt + 1)
                    logger.warning(
                        "HTTP %s for %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code,
                        url,
                        wait,
                        attempt + 1,
                        self.retries,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:  # network / timeout / 4xx-5xx
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
        raise httpx.HTTPError(f"Failed to fetch {url} after {self.retries + 1} attempts") from last_exc

    def fetch_text(self, url: str) -> str:
        return self.fetch(url).text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
