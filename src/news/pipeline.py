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

# 抓取失败时写入 body_text 的前缀（与 storage.mark_failed 保持一致）
_FAILURE_PREFIX = "[抓取失败]"

# 正文可读性最小字符数（空标题/空正文/仅失败占位不能成为 usable article）
_MIN_READABLE_BODY_CHARS = 10

# RFI 候选池相对 usable limit 的放大系数。
# 语义分离：``limit``（``Pipeline.max_items``）表示**目标 usable 数量**；
# discovery 返回的 **candidate pool** 应大于 usable 目标，这样即使部分候选
# 抓取失败，也能从近期候选里凑够 usable，而不必向历史文章扩展。
# 由于 RFI discovery 已带 7 天时间窗口，candidate pool 天然有时间边界，
# 不会因放大而无限制地向更老文章寻找。
RFI_CANDIDATE_MULTIPLIER = 3

# 音频节目/播音页标题特征。RFI 等站点存在“第一次播音 06:00 - 07:00”这类
# 音频节目页，其正文质量差（只有节目描述），不能作为普通新闻。
# 此类页面若正文过短（低于 _AUDIO_PROGRAM_MIN_BODY_CHARS）则判为 low_quality。
_AUDIO_PROGRAM_TITLE_PATTERNS = (
    "第一次播音",
    "第二次播音",
    "第三次播音",
    "广播",
    "播音",
    "节目",
    "电台",
)

# 音频节目页正文最小长度（字符）。低于此阈值视为“仅节目介绍”，判为 low_quality。
_AUDIO_PROGRAM_MIN_BODY_CHARS = 200

# 节目/广播页关键词（供站点配置 quality_program_keywords 的默认参考）。
# 实际生效值来自站点 yaml；此处为代码级兜底。
_PROGRAM_KEYWORDS_DEFAULT = _AUDIO_PROGRAM_TITLE_PATTERNS


def _is_audio_program_page(title: str) -> bool:
    """判断标题是否命中音频节目/播音页特征。"""
    t = (title or "").strip().lower()
    return any(p.lower() in t for p in _AUDIO_PROGRAM_TITLE_PATTERNS)


def _html_to_plain(html: str) -> str:
    """简单去 HTML 标签得到纯文本（供正文质量校验）。"""
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _body_length(article) -> int:
    """返回正文去除空白后的字符数。"""
    import re

    body_text = (article.body_text or "").strip()
    if body_text:
        return len(re.sub(r"\s+", "", body_text))
    return len(re.sub(r"\s+", "", _html_to_plain(article.body_html or "")))


def _assess_low_quality(article, quality_cfg: dict | None = None) -> tuple[bool, str]:
    """评估文章是否判为 low_quality（正文质量偏低，不入普通新闻池）。

    返回 ``(is_low, reason)``。

    判定规则（RFI 专用，通过站点配置驱动，不污染通用源）：
    - ``quality_min_chars``：正文去除空白后字符数低于该阈值 → low（"正文过短"）；
    - ``quality_program_keywords``：标题命中节目/播音/广播关键词 → low（"疑似节目/广播页"）；
    - 内置音频节目页检测：标题命中播音/广播/节目特征且正文过短（< 200）→ low。

    仅当站点配置或内置检测触发时才判 low；ECO / HKEJ 等通用源未配置
    quality 参数时恒为 ``(False, "")``，保持原有行为。
    """
    q = quality_cfg or {}
    min_chars = q.get("min_chars")
    program_keywords = tuple(q.get("program_keywords") or ())

    title = (article.title or "").strip()
    body_len = _body_length(article)

    if min_chars is not None:
        if body_len < min_chars:
            return True, f"正文过短（{body_len} 字符 < {min_chars}）"

    if program_keywords:
        if title:
            for kw in program_keywords:
                if kw in title:
                    return True, f"疑似节目/广播页（标题命中: {kw}）"

    # 内置音频节目页检测：标题命中节目特征且正文过短（仅节目介绍）→ low。
    if _is_audio_program_page(title):
        if body_len < _AUDIO_PROGRAM_MIN_BODY_CHARS:
            return True, f"音频节目页正文过短（{body_len} 字），仅节目介绍，不视为可读新闻"

    return False, ""


def _validate_extracted_body(article) -> bool:
    """校验已提取的文章是否具备可读正文与标题。

    至少满足：标题非空，且 ``body_html`` 或 ``body_text`` 真正有可读内容
    （非空、非失败占位前缀、长度达到阈值）。返回 True 表示可作为 usable article。
    """
    title = (article.title or "").strip()
    if not title:
        return False
    body_text = (article.body_text or "").strip()
    body_html = (article.body_html or "").strip()
    if not body_text and not body_html:
        return False
    if body_text.startswith(_FAILURE_PREFIX) or body_html.startswith(_FAILURE_PREFIX):
        return False
    # 取正文文本长度（body_html 存在时也确保有可读文本）
    if body_text:
        readable = body_text
    else:
        readable = _html_to_plain(body_html)
    return len(readable.strip()) >= _MIN_READABLE_BODY_CHARS


def _invalid_body_reason(article) -> str:
    """返回正文质量校验失败的原因（用于日志 / mark_failed）。"""
    title = (article.title or "").strip()
    if not title:
        return "空标题，不视为可用正文"
    body_text = (article.body_text or "").strip()
    body_html = (article.body_html or "").strip()
    if not body_text and not body_html:
        return "空正文，不视为可用正文"
    if body_text.startswith(_FAILURE_PREFIX) or body_html.startswith(_FAILURE_PREFIX):
        return "正文为失败占位，不视为可用正文"
    if body_text:
        readable = body_text
    else:
        readable = _html_to_plain(body_html)
    if len(readable.strip()) < _MIN_READABLE_BODY_CHARS:
        return f"正文过短（{len(readable.strip())} 字），不视为可用正文"
    return "正文质量不达标"


@dataclass
class FetchStats:
    discovered: int = 0
    skipped_dup: int = 0
    fetched_ok: int = 0
    extracted_ok: int = 0
    low_quality: int = 0  # 正文质量偏低（节目/短文），不入普通新闻池
    failed: int = 0
    usable: int = 0  # 通过正文质量验证、可读新闻数（usable article）
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "skipped_dup": self.skipped_dup,
            "fetched_ok": self.fetched_ok,
            "extracted_ok": self.extracted_ok,
            "low_quality": self.low_quality,
            "failed": self.failed,
            "usable": self.usable,
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

        # 站点级正文节流：把 rfi.yaml 的 article_interval 应用到共享 fetcher。
        # discovery 仍走普通 fetch()；只有正文 fetch_article() 使用该独立间隔。
        # 必须总是重置（包括 None），否则上一个站点的 interval 会残留；
        # 同时清理上次站点的文章节流记录，避免跨站点串扰（ECO/HKEJ 保持 None）。
        article_interval = cfg.get("article_interval")
        if hasattr(self.fetcher, "article_interval"):
            self.fetcher.article_interval = (
                float(article_interval) if article_interval is not None else None
            )
        # 站点切换时清理文章节流 bucket（避免上一个站点的记录影响当前站点）
        if hasattr(self.fetcher, "_last_article_request"):
            self.fetcher._last_article_request.clear()

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
        # 获取数据库已有的 canonical URLs，让 discovery 层跳过已入库文章
        # （重复文章不消耗 limit 名额）。
        existing_urls = self.storage.all_canonical_urls(source_id=source_id)
        # candidate pool 与 usable limit 分离：
        # - ``target_usable``（= ``self.max_items``）是本次希望得到的可读新闻数；
        # - discovery 的 ``max_items`` 是 **candidate pool 上限**。
        # RFI 因 discovery 已带 7 天时间窗口，可安全放大候选池，给失败候选留缓冲，
        # 且不会向历史文章扩展；ECO/HKEJ 等站点保持原有候选上限不变。
        discovery_limit = self.max_items
        if cfg.get("adapter") == "rfi":
            discovery_limit = max(
                self.max_items * RFI_CANDIDATE_MULTIPLIER,
                self.max_items,
            )
        items = discover_for_site(
            cfg,
            fetcher=self.fetcher,
            max_items=discovery_limit,
            existing_urls=existing_urls,
        )
        stats.discovered = len(items)
        logger.info(
            "[%s] 发现 %d 条候选文章（已过滤 %d 条数据库已有）",
            source_id,
            len(items),
            len(existing_urls),
        )

        # 2. 逐个：去重 → 下载 → 提取 → 入库，直到 usable >= max_items 或候选耗尽
        # 正文质量门槛（RFI 专用，配置化）：quality_min_chars / quality_program_keywords。
        quality_cfg = {
            "min_chars": cfg.get("quality_min_chars"),
            "program_keywords": tuple(cfg.get("quality_program_keywords") or ()),
        }
        ingest = self._ingest_items(
            items,
            source_id,
            source_name,
            language,
            site_extract,
            title_suffixes,
            fetch_headers=custom_headers,
            adapter=adapter,
            target_usable=self.max_items,
            quality_cfg=quality_cfg,
        )
        ingest.discovered = len(items)

        # 3. limit 达成情况报告
        if ingest.usable >= self.max_items:
            logger.info(
                "[%s] 已达到 limit=%d（新增可读新闻 %d）",
                source_id,
                self.max_items,
                ingest.usable,
            )
        elif ingest.usable < self.max_items and items:
            # 未能达到 limit：所有来源已耗尽（候选不足，不是抓取失败）
            logger.warning(
                "[%s] 候选已耗尽，可读新闻 %d / 目标 %d（候选 %d 条）。"
                "跳过重复 %d，正文成功 %d，质量不合格 %d，抓取/提取失败 %d。"
                "原因：候选不足、数据库重复过多、或正文质量不达标，而非抓取失败。",
                source_id,
                ingest.usable,
                self.max_items,
                ingest.discovered,
                ingest.skipped_dup,
                ingest.fetched_ok,
                ingest.low_quality,
                ingest.failed,
            )
        elif not items:
            logger.warning(
                "[%s] 所有来源已耗尽：无可发现的新候选（limit=%d），可能是数据库已包含全部可用文章",
                source_id,
                self.max_items,
            )

        return ingest

    def refresh_source(self, source_id: str, *, limit: int = 1000) -> FetchStats:
        """重新抓取并更新已有文章，不插入新行。

        这是给 source adapter 修复历史正文使用的安全入口：文章通过现有
        ``source_id`` + canonical URL 已经存在于 SQLite，刷新只调用
        ``update_article_body``，不会经过 discovery 或 ``insert_article``。
        当前主要用于把 NYT 早期误存的 RSS summary 替换为文章页正文。
        """
        cfg = load_site_config(source_id)
        from .sources import get_adapter

        adapter = get_adapter(cfg)
        if adapter is None:
            raise ValueError(f"站点 {source_id!r} 没有可用于 refresh 的 source adapter")
        articles = self.storage.list_articles(source_id=source_id, limit=limit)
        stats = FetchStats(discovered=len(articles))
        for article in articles:
            if not article.id or not article.canonical_url:
                continue
            try:
                html = self.fetcher.fetch_article(article.canonical_url)
                refreshed = Article(
                    source_id=article.source_id,
                    source_name=article.source_name,
                    canonical_url=article.canonical_url,
                    title=article.title,
                    authors=list(article.authors),
                    published_at=article.published_at,
                    discovered_at=article.discovered_at,
                    id=article.id,
                )
                if not adapter.extract_article(refreshed, html, url=article.canonical_url):
                    raise ValueError("adapter 未提取到可读正文")
                if not _validate_extracted_body(refreshed):
                    raise ValueError(_invalid_body_reason(refreshed))
                refreshed.fetched_at = utcnow()
                refreshed.status = "fetched"
                self._update_body(article.id, refreshed, source_id, stats, logger)
                stats.fetched_ok += 1
                stats.extracted_ok += 1
                stats.usable += 1
                logger.info("[%s] 刷新成功 #%d  %s", source_id, article.id, refreshed.title[:60])
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"#{article.id} {article.canonical_url}: {exc}")
                logger.error("[%s] 刷新失败 #%d: %s", source_id, article.id, exc)
        return stats

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
        target_usable: int | None = None,
        quality_cfg: dict | None = None,
    ) -> FetchStats:
        """处理一批已发现条目：去重 → 下载 → 提取 → 入库。

        独立成方法便于测试注入离线抓取器。

        ``target_usable``：目标 usable 文章数上限。当 ``stats.usable >=
        target_usable`` 时提前停止处理剩余候选（已达到 limit 目标）。
        为 None 时处理所有候选（兼容旧测试直接调用）。

        ``quality_cfg``：站点正文质量门槛（quality_min_chars /
        quality_program_keywords）。命中 → 判为 low_quality（不入普通新闻池，
        与 failed 明确区分）。未配置则保持原行为。

        新增 discovery content short-circuit：若 RSS 条目已带完整正文
        （``has_usable_content(item.content_html)`` 为 True），直接用它作为
        正文，跳过 ``fetcher.fetch()`` 与 ``extract()``（0 次额外请求）；
        否则照常 fetch 原文 URL + extract（含站点 adapter 的 HTML fallback）。

        已存在 URL（数据库重复）不消耗 limit；正文失败 / 空正文 / 空标题
        不消耗 limit；只有真正 usable（通过质量验证）才计数。
        """
        stats = FetchStats()
        site_extract = site_extract or {}

        for item in items:
            # 已达目标 usable 数 → 提前停止（不消耗 limit 的候选被跳过）
            if target_usable is not None and stats.usable >= target_usable:
                logger.info(
                    "[%s] 已达目标 %d 篇可读新闻，停止处理剩余候选",
                    source_id, target_usable,
                )
                break

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
                # RSS 完整正文同样走质量判定（节目/短文不入普通池）。
                low, low_reason = _assess_low_quality(article, quality_cfg)
                if low:
                    stats.low_quality += 1
                    article.status = "low_quality"
                    self.storage.mark_low_quality(article_id)
                    logger.warning(
                        "[%s] 正文质量偏低（RSS），不入普通新闻池 #%d  %s（%s）",
                        source_id, article_id, article.title[:50], low_reason,
                    )
                    continue
                self._update_body(
                    article_id, article, source_id, stats, logger
                )
                stats.usable += 1
                logger.info(
                    "[%s] 入库成功 #%d  %s（RSS 完整正文，0 fetch）",
                    source_id, article_id, article.title[:60],
                )
                continue

            # 正文请求使用 fetch_article()（独立文章节流）；discovery 仍用 fetch()
            try:
                if fetch_headers:
                    html = self.fetcher.fetch_article(canon, headers=fetch_headers)
                else:
                    html = self.fetcher.fetch_article(canon)
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

            # 4.5 正文结构验证：标题非空 + 正文有可读内容。
            # 空标题 / 空正文 / [抓取失败] 前缀 都不能成为 usable article（→ failed）。
            if not _validate_extracted_body(article):
                stats.failed += 1
                err = _invalid_body_reason(article)
                stats.errors.append(f"{canon}: {err}")
                logger.error("[%s] %s", source_id, err)
                self.storage.mark_failed(article_id, error=err)
                continue

            # 4.6 正文质量判定（RFI 专用，配置化）：命中 → low_quality（与 failed 区分）。
            low, low_reason = _assess_low_quality(article, quality_cfg)
            if low:
                stats.low_quality += 1
                article.status = "low_quality"
                self.storage.mark_low_quality(article_id)
                logger.warning(
                    "[%s] 正文质量偏低，不入普通新闻池 #%d  %s（%s）",
                    source_id, article_id, article.title[:50], low_reason,
                )
                continue

            # 5. 回填（通过验证 → 成为 usable article）
            self._update_body(article_id, article, source_id, stats, logger)
            stats.usable += 1
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
            # 重试同样按站点应用正文节流（总是重置，包括 None；并清理节流 bucket）
            art_interval = cfg.get("article_interval")
            if hasattr(self.fetcher, "article_interval"):
                self.fetcher.article_interval = (
                    float(art_interval) if art_interval is not None else None
                )
            if hasattr(self.fetcher, "_last_article_request"):
                self.fetcher._last_article_request.clear()
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
            quality_cfg = {
                "min_chars": cfg.get("quality_min_chars"),
                "program_keywords": tuple(cfg.get("quality_program_keywords") or ()),
            }
            for art in articles:
                try:
                    # 重试同样使用 fetch_article()（独立文章节流）
                    if fetch_headers:
                        html = self.fetcher.fetch_article(art.canonical_url, headers=fetch_headers)
                    else:
                        html = self.fetcher.fetch_article(art.canonical_url)
                    art.fetched_at = utcnow()
                    if fetch_headers:
                        from .extract import apply_site_adapter_extraction

                        apply_site_adapter_extraction(art, html)
                    else:
                        apply_extraction_to_article(art, html, site_extract=site_extract)
                    # 重试后同样验证正文结构（空标题/空正文/失败占位 → failed）
                    if not _validate_extracted_body(art):
                        stats.failed += 1
                        err = _invalid_body_reason(art)
                        stats.errors.append(f"{art.canonical_url}: {err}")
                        logger.error("[%s] %s", sid, err)
                        self.storage.mark_failed(art.id, error=err)  # type: ignore[arg-type]
                        continue
                    # 重试后同样做质量判定（节目/短文 → low_quality，与 failed 区分）
                    low, low_reason = _assess_low_quality(art, quality_cfg)
                    if low:
                        stats.low_quality += 1
                        art.status = "low_quality"
                        self.storage.mark_low_quality(art.id)  # type: ignore[arg-type]
                        logger.warning(
                            "[%s] 重试后质量偏低，不入普通池 %s（%s）",
                            sid, art.canonical_url, low_reason,
                        )
                        continue
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
                    stats.usable += 1
                except Exception as exc:
                    stats.failed += 1
                    err = f"重试失败: {exc}"
                    stats.errors.append(f"{art.canonical_url}: {err}")
                    logger.error("[%s] %s", sid, err)
        return stats
