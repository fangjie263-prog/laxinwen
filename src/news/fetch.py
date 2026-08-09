"""抓取层 —— 下载网页。

默认 HTTPX；Fetcher 抽象允许以后方便增加 PlaywrightFetcher
（仅当目标站点必须 JS 渲染时才启用）。
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "laxinwen/0.1 (personal news research tool)"
)

# 常见希望获取 JSON 而非 HTML 的站点（RSSHub 等）也统一处理
_HTML_HINTS = ("text/html", "application/xhtml", "text/xml", "application/xml", "text/plain")


@dataclass
class FetcherOptions:
    timeout: float = 20.0
    retries: int = 3
    retry_backoff: float = 1.5          # 指数退避基数（秒）
    min_interval: float = 2.0           # 同一域名两次请求的最小间隔（秒）
    max_interval: float = 8.0           # 随机化上限
    user_agent: str = DEFAULT_USER_AGENT
    headers: dict[str, str] = field(default_factory=dict)
    respect_retry_after: bool = True


class FetchError(Exception):
    """抓取失败（HTTP 错误 / 超时 / 网络错误）。"""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class BaseFetcher(ABC):
    """Fetcher 抽象。未来 PlaywrightFetcher 实现同一接口。"""

    @abstractmethod
    def fetch(self, url: str, **kwargs) -> str:
        """下载 URL 并返回文本（HTML/XML/JSON 字符串）。"""

    @abstractmethod
    def close(self) -> None:  # pragma: no cover - 接口定义
        ...


class HttpxFetcher(BaseFetcher):
    """基于 httpx 的抓取器，带 retry / 超时 / 域名请求间隔 / Retry-After 尊重。"""

    def __init__(self, options: Optional[FetcherOptions] = None):
        self.options = options or FetcherOptions()
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            timeout=self.options.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.options.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/rss+xml,application/atom+xml,application/json;q=0.8,*/*;q=0.7"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,pt;q=0.7",
                **self.options.headers,
            },
        )

    # ---------- 抓取礼仪 ----------

    def _throttle(self, url: str) -> None:
        """同一域名最小请求间隔 + 随机抖动。"""
        host = httpx.URL(url).host or url
        last = self._last_request.get(host, 0.0)
        elapsed = time.monotonic() - last
        wait = self.options.min_interval - elapsed
        if wait > 0:
            # 在 [min_interval, max_interval] 之间随机抖动，避免规律性请求
            jitter = random.uniform(0, self.options.max_interval - self.options.min_interval)
            logger.debug("throttle %.2fs (host=%s)", wait + jitter, host)
            time.sleep(wait + jitter)
        self._last_request[host] = time.monotonic()

    def _respect_retry_after(self, response: httpx.Response, backoff: float) -> float:
        if not self.options.respect_retry_after:
            return backoff
        ra = response.headers.get("Retry-After")
        if ra:
            try:
                return max(float(ra), backoff)
            except ValueError:
                pass
        return backoff

    # ---------- 主入口 ----------

    def fetch(self, url: str, **kwargs) -> str:
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        for attempt in range(1, self.options.retries + 1):
            self._throttle(url)
            try:
                resp = self._client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                logger.warning("[%d/%d] %s 网络错误: %s", attempt, self.options.retries, url, exc)
            else:
                if resp.status_code == 200:
                    return resp.text
                # 5xx / 429 重试；4xx 不重试
                if resp.status_code in (429,) or resp.status_code >= 500:
                    last_exc = FetchError(
                        f"HTTP {resp.status_code}",
                        status=resp.status_code,
                    )
                    last_status = resp.status_code
                    logger.warning(
                        "[%d/%d] %s HTTP %s，稍后重试",
                        attempt, self.options.retries, url, resp.status_code,
                    )
                else:
                    raise FetchError(
                        f"HTTP {resp.status_code} for {url}", status=resp.status_code
                    )
            if attempt < self.options.retries:
                backoff = self.options.retry_backoff * (2 ** (attempt - 1))
                if isinstance(last_exc, FetchError) and last_status is not None:
                    # 模拟最后一次响应用于 Retry-After（不可得时忽略）
                    pass
                wait = backoff + random.uniform(0, 1)
                logger.debug("retry in %.1fs", wait)
                time.sleep(wait)
        raise FetchError(
            f"抓取失败（重试 {self.options.retries} 次后）: {url}"
            + (f" 最后状态 {last_status}" if last_status else "")
        ) from last_exc

    def close(self) -> None:
        self._client.close()
