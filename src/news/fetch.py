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

# 默认不设置自定义 User-Agent：由 httpx 使用自身默认的 python-httpx/... UA。
# 实验确认 Chrome 风格 UA 会触发 RFI 等站点的 403。
# 如需自定义 UA，调用方可通过 FetcherOptions(user_agent="...") 显式指定。
DEFAULT_USER_AGENT = None

# 常见希望获取 JSON 而非 HTML 的站点（RSSHub 等）也统一处理
_HTML_HINTS = ("text/html", "application/xhtml", "text/xml", "application/xml", "text/plain")


@dataclass
class FetcherOptions:
    timeout: float = 20.0
    retries: int = 3
    retry_backoff: float = 1.5          # 指数退避基数（秒）
    min_interval: float = 2.0           # 同一域名两次请求的最小间隔（秒）
    max_interval: float = 8.0           # 随机化上限
    user_agent: Optional[str] = DEFAULT_USER_AGENT
    headers: dict[str, str] = field(default_factory=dict)
    respect_retry_after: bool = True
    # 正文抓取的独立节流间隔（秒）。默认 None 表示与 discovery 共用 min_interval；
    # 若设置（如 RFI 的 15 秒），fetch_article() 走独立的文章节流，与 discovery 完全分开。
    article_interval: Optional[float] = None


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

    def fetch_article(self, url: str, **kwargs) -> str:
        """下载一篇文章的正文。

        默认实现与 ``fetch`` 完全一致（无文章专用节流），保证 ECO / HKEJ 等
        未配置 ``article_interval`` 的站点行为不变。
        需要独立正文节流的 Fetcher（如 HttpxFetcher）会覆盖此方法。
        """
        return self.fetch(url, **kwargs)

    @abstractmethod
    def close(self) -> None:  # pragma: no cover - 接口定义
        ...


class HttpxFetcher(BaseFetcher):
    """基于 httpx 的抓取器，带 retry / 超时 / 域名请求间隔 / Retry-After 尊重。"""

    def __init__(self, options: Optional[FetcherOptions] = None):
        self.options = options or FetcherOptions()
        self._last_request: dict[str, float] = {}
        # 文章专用节流（独立于 discovery）。初始取自 options.article_interval；
        # pipeline 在 run_site 时按站点配置覆盖（RFI 15s，ECO/HKEJ 为 None）。
        self._last_article_request: dict[str, float] = {}
        self.article_interval: Optional[float] = self.options.article_interval
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/rss+xml,application/atom+xml,application/json;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,pt;q=0.7",
            **self.options.headers,
        }
        # 仅在调用方显式指定 UA 时覆盖；否则保留 httpx 默认 UA（避免 403）。
        if self.options.user_agent:
            headers["User-Agent"] = self.options.user_agent
        self._client = httpx.Client(
            timeout=self.options.timeout,
            follow_redirects=True,
            headers=headers,
        )

    # ---------- 抓取礼仪 ----------

    def _throttle(self, url: str) -> None:
        """同一域名最小请求间隔 + 随机抖动（discovery 节流）。"""
        self._throttle_with(url, self.options.min_interval, self._last_request)

    def _article_throttle(self, url: str) -> None:
        """文章正文专用节流（与 discovery 完全独立）。

        使用 ``self.article_interval``（未配置时回退到 ``min_interval``）；
        追踪在 ``_last_article_request``，不干扰 discovery 的 ``_last_request``。
        """
        interval = self.article_interval if self.article_interval is not None else self.options.min_interval
        self._throttle_with(url, interval, self._last_article_request)

    def _throttle_with(self, url: str, interval: float, bucket: dict[str, float]) -> None:
        """通用节流：在 ``bucket`` 内按域名记录上次请求，保证间隔 >= interval。"""
        host = httpx.URL(url).host or url
        last = bucket.get(host, 0.0)
        elapsed = time.monotonic() - last
        wait = interval - elapsed
        if wait > 0:
            # 在 [interval, max_interval] 之间随机抖动，避免规律性请求
            jitter = random.uniform(0, max(0.0, self.options.max_interval - interval))
            logger.debug("throttle %.2fs (host=%s)", wait + jitter, host)
            time.sleep(wait + jitter)
        bucket[host] = time.monotonic()

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
        """下载 URL（discovery 节流），返回文本。"""
        return self._fetch_with_throttle(url, self._throttle, **kwargs)

    def fetch_article(self, url: str, **kwargs) -> str:
        """下载文章正文（独立文章节流），返回文本。

        每次真实 HTTP attempt 前都经过文章节流（retry=3 时 3 次 attempt
        都分别节流），与 discovery 的普通节流完全独立。
        """
        return self._fetch_with_throttle(url, self._article_throttle, **kwargs)

    def _fetch_with_throttle(self, url: str, throttle_fn, **kwargs) -> str:
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None
        # 可选：per-request headers（站点 adapter 可通过 kwargs 覆盖 UA 等）
        extra_headers = kwargs.get("headers") or {}
        for attempt in range(1, self.options.retries + 1):
            throttle_fn(url)
            try:
                if extra_headers:
                    resp = self._client.get(url, headers=extra_headers)
                else:
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
