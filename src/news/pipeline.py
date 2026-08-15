"""抓取 Pipeline —— 将发现、下载、提取、去重、入库串起来。

单篇失败不会中断整个任务：
- 成功文章正常入库
- 失败文章记录 status='failed'，任务继续
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import load_site_config
from .discover import discover_for_site, has_usable_content
from .extract import apply_extraction_to_article
from .fetch import FetcherOptions, HttpxFetcher
from .model import Article, utcnow
from .normalize import canonicalize_url, fingerprint_sha256
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class FetchStats:
    discovered: int = 0
    skipped_dup: int = 0
    fetched_ok: int = 0
    extracted_ok: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "skipped_dup": self.skipped_dup,
            "fetched_ok": self.fetched_ok,
            "extracted_ok": self.extracted_ok,
            "failed": self.failed,
            "errors": self.errors,
        }


class Pipeline:
    """一站式新闻抓取 pipeline。"""

    def __init__(
        self,
        storage: Storage,
        *,
        fetcher: Optional[HttpxFetcher] = None,
        max_items: int = 30,
        fetch_articles: bool = True,
    ):
        self.storage = storage
        self.fetcher = fetcher or HttpxFetcher(FetcherOptions())
        self.max_items = max_items
        self.fetch_articles = fetch_articles

    def close(self) -> None:
        self.fetcher.close()

    # ---------- 站点级抓取 ----------

    def run_site(self, site_id: str) -> FetchStats:
        cfg = load_site_config(site_id)
        stats = FetchStats()

        source_id = cfg.get("id", site_id)
        source_name = cfg.get("name", source_id)
        language = cfg.get("language", "")
        site_extract = cfg.get("extract") or {}
        title_suffixes = cfg.get("title_suffixes") or []

        # 站点 adapter（HKEJ 等）可提供自定义请求头（浏览器 UA 等）
        custom_headers: dict | None = None
        adapter = None
        if cfg.get("adapter"):
            from .sources import get_adapter

            adapter = get_adapter(cfg)
            if adapter is not None:
                custom_headers = adapter.fetch_custom_headers()

        # 1. 发现
        items = discover_for_site(cfg, fetcher=self.fetcher, max_items=self.max_items)
        stats.discovered = len(items)
        logger.info("[%s] 发现 %d 条候选文章", source_id, len(items))

        # 2. 逐个：去重 → 下载 → 提取 → 入库
        ingest = self._ingest_items(
            items,
            source_id,
            source_name,
            language,
            site_extract,
            title_suffixes,
            fetch_headers=custom_headers,
            adapter=adapter,
        )
        ingest.discovered = len(items)
        return ingest

    def _ingest_items(
        self,
        items: list,
        source_id: str,
        source_name: str,
        language: str,
        site_extract: dict | None,
        title_suffixes: list[str],
        fetch_headers: dict | None = None,
        adapter=None,
    ) -> FetchStats:
        """处理一批已发现条目：去重 → 下载 → 提取 → 入库。

        独立成方法便于测试注入离线抓取器。

        新增 discovery content short-circuit：若 RSS 条目已带完整正文
        （``has_usable_content(item.content_html)`` 为 True），直接用它作为
        正文，跳过 ``fetcher.fetch()`` 与 ``extract()``（0 次额外请求）；
        否则照常 fetch 原文 URL + extract（含站点 adapter 的 HTML fallback）。
        """
        stats = FetchStats()
        site_extract = site_extract or {}

        for item in items:
            article = item.to_article(source_id, source_name, language=language)
            canon = canonicalize_url(article.canonical_url)

            # 第一层去重：canonical URL
            if self.storage.url_exists(canon):
                stats.skipped_dup += 1
                logger.debug("[%s] 跳过重复 URL: %s", source_id, canon)
                continue

            # 第二层去重：标题指纹（与同源已存在文章比对）
            fp = fingerprint_sha256(article.title, site_suffixes=title_suffixes)
            if fp and self.storage.title_fp_exists(source_id, fp):
                stats.skipped_dup += 1
                logger.debug("[%s] 跳过重复标题指纹: %s", source_id, article.title)
                continue

            article_id, inserted = self.storage.insert_article(article, title_fp=fp)
            if not inserted:
                stats.skipped_dup += 1
                continue

            if not self.fetch_articles:
                continue

            # 3. 下载正文
            fetched_at = utcnow()

            # --- discovery content short-circuit ---
            # RSS 已带完整正文（content_html）时，直接作为正文，跳过 fetch + extract
            if has_usable_content(item.content_html):
                from .discover import html_to_text

                article.body_html = item.content_html
                article.body_text = html_to_text(item.content_html)
                article.fetched_at = fetched_at
                article.status = "fetched"
                stats.fetched_ok += 1
                stats.extracted_ok += 1
                self._update_body(
                    article_id, article, source_id, stats, logger
                )
                logger.info(
                    "[%s] 入库成功 #%d  %s（RSS 完整正文，0 fetch）",
                    source_id, article_id, article.title[:60],
                )
                continue

            try:
                if fetch_headers:
                    html = self.fetcher.fetch(canon, headers=fetch_headers)
                else:
                    html = self.fetcher.fetch(canon)
                article.fetched_at = fetched_at
                article.status = "fetched"
                stats.fetched_ok += 1
            except Exception as exc:
                stats.failed += 1
                err = f"下载失败: {exc}"
                stats.errors.append(f"{canon}: {err}")
                logger.error("[%s] %s", source_id, err)
                self.storage.mark_failed(article_id, error=err)
                continue

            # 4. 正文提取
            try:
                # 站点 adapter 的 HTML fallback 优先（RFI .t-content__body 等）
                if adapter is not None and adapter.extract_article(article, html, url=canon):
                    pass  # adapter 已回填 body_html/body_text 等
                elif fetch_headers:
                    # 站点 adapter（HKEJ）用 ResearchReader 已验证的解析逻辑
                    from .extract import apply_site_adapter_extraction

                    apply_site_adapter_extraction(article, html)
                else:
                    apply_extraction_to_article(article, html, site_extract=site_extract)
                article.fetched_at = fetched_at
                article.status = "fetched"
                stats.extracted_ok += 1
            except Exception as exc:
                stats.failed += 1
                err = f"提取失败: {exc}"
                stats.errors.append(f"{canon}: {err}")
                logger.error("[%s] %s", source_id, err)
                self.storage.mark_failed(article_id, error=err)
                continue

            # 5. 回填
            self._update_body(article_id, article, source_id, stats, logger)
            logger.info(
                "[%s] 入库成功 #%d  %s", source_id, article_id, article.title[:60]
            )

        return stats

    def _update_body(self, article_id, article, source_id, stats, logger):
        """把已提取的正文回填到数据库（供 short-circuit / 普通路径复用）。"""
        self.storage.update_article_body(
            article_id,
            title=article.title,
            authors=article.authors,
            body_text=article.body_text,
            body_html=article.body_html,
            images=article.images,
            lead_image=article.lead_image,
            published_at=article.published_at,
            fetched_at=article.fetched_at,
            language=article.language,
            status=article.status,
        )

    # ---------- 重试失败项 ----------

    def retry_failed(self, source_id: Optional[str] = None) -> FetchStats:
        """重新抓取 status='failed' 的文章。"""
        stats = FetchStats()
        failed = self.storage.list_articles(
            status="failed", source_id=source_id, limit=1000
        )
        if not failed:
            logger.info("没有需要重试的失败文章")
            return stats
        # 按 source 分组
        by_source: dict[str, list[Article]] = {}
        for art in failed:
            by_source.setdefault(art.source_id, []).append(art)
        for sid, articles in by_source.items():
            try:
                cfg = load_site_config(sid)
            except FileNotFoundError:
                cfg = {"id": sid, "name": sid}
            site_extract = cfg.get("extract") or {}
            # 重试同样使用站点 adapter 的自定义请求头（HKEJ 浏览器 UA 等）
            fetch_headers: dict | None = None
            if cfg.get("adapter"):
                try:
                    from .sources import get_adapter

                    adapter = get_adapter(cfg)
                    if adapter is not None:
                        fetch_headers = adapter.fetch_custom_headers()
                except Exception:
                    fetch_headers = None
            for art in articles:
                try:
                    if fetch_headers:
                        html = self.fetcher.fetch(art.canonical_url, headers=fetch_headers)
                    else:
                        html = self.fetcher.fetch(art.canonical_url)
                    art.fetched_at = utcnow()
                    if fetch_headers:
                        from .extract import apply_site_adapter_extraction

                        apply_site_adapter_extraction(art, html)
                    else:
                        apply_extraction_to_article(art, html, site_extract=site_extract)
                    art.status = "fetched"
                    self.storage.update_article_body(
                        art.id,  # type: ignore[arg-type]
                        title=art.title,
                        authors=art.authors,
                        body_text=art.body_text,
                        body_html=art.body_html,
                        images=art.images,
                        lead_image=art.lead_image,
                        published_at=art.published_at,
                        fetched_at=art.fetched_at,
                        language=art.language,
                        status="fetched",
                    )
                    stats.fetched_ok += 1
                    stats.extracted_ok += 1
                except Exception as exc:
                    stats.failed += 1
                    err = f"重试失败: {exc}"
                    stats.errors.append(f"{art.canonical_url}: {err}")
                    logger.error("[%s] %s", sid, err)
        return stats
