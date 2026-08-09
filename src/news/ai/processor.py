"""Article AI 处理器 —— 编排 读取 → 调 AI → 校验 → 入库。

关键行为：
- 默认只处理"已成功抓取但还没有 AI analysis"的文章（避免重复消费 API）。
- --retry-failed 时额外重试 status='failed' 的分析。
- 单篇失败不中断整个 batch：记录 error，继续处理下一篇。
- Article 本身不因 AI 失败而丢失（只写 article_analysis 记录）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..model import Article
from ..storage import Storage
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .provider import AIProviderConfig, AIProviderError, BaseProvider
from .schema import AnalysisValidationError, ArticleAnalysis, extract_json_object, validate_analysis

logger = logging.getLogger(__name__)

# JSON 解析失败后的有限重试次数
MAX_PARSE_RETRIES = 2


@dataclass
class ProcessResult:
    """单篇文章的处理结果。"""

    article_id: int
    ok: bool = False
    status: str = "error"           # success / skipped / error
    error: str = ""
    prompt_version: str = PROMPT_VERSION
    usage: dict = field(default_factory=dict)
    model: str = ""


@dataclass
class BatchStats:
    """一批处理结果的统计。"""

    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class ArticleProcessor:
    """把 Article 交给 AI Provider，校验后写入 article_analysis。"""

    def __init__(
        self,
        storage: Storage,
        provider: Optional[BaseProvider] = None,
        *,
        config: Optional[AIProviderConfig] = None,
        max_parse_retries: int = MAX_PARSE_RETRIES,
    ):
        self.storage = storage
        if provider is None:
            from .provider import build_provider

            self.config = config or AIProviderConfig.from_env()
            self.provider = build_provider(self.config)
        else:
            self.config = config or AIProviderConfig()
            self.provider = provider
        self.max_parse_retries = max_parse_retries

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()

    # ---------- 单篇处理 ----------

    def process_article(self, article: Article) -> ProcessResult:
        """处理单篇文章。失败返回 error 记录，不抛异常。

        retry 策略（受 ``self.max_parse_retries`` 限制，不会无限重试）：
        - Provider 层失败（网络 / HTTP / SSE 解析 / JSON 解析 / 空响应）会重试；
        - JSON 解析 / schema 校验失败会重试（追加错误说明重新调用模型）；
        - 最终仍失败则记录 ``status='failed'`` + error，并返回。
        """
        system = SYSTEM_PROMPT
        user = build_user_prompt(article)
        result = ProcessResult(article_id=article.id or 0)

        for attempt in range(self.max_parse_retries + 1):
            try:
                provider_result = self.provider.chat(system=system, user=user)
            except AIProviderError as exc:
                # Provider / SSE / 空响应失败：有限次重试
                if attempt < self.max_parse_retries:
                    logger.warning(
                        "[#%s] 第 %d 次 AI 调用失败，重试: %s",
                        article.id, attempt + 1, exc,
                    )
                    continue
                result.error = f"AI 调用失败: {exc}"
                logger.error("[#%s] %s", article.id, result.error)
                self._save_failure(article, result.error, result.model)
                return result

            result.model = provider_result.model
            result.usage = provider_result.usage

            # JSON 解析与 schema 校验
            try:
                obj = extract_json_object(provider_result.content)
                analysis = validate_analysis(obj)
            except (AnalysisValidationError, ValueError) as exc:
                # 有限次 retry：让模型重新输出
                if attempt < self.max_parse_retries:
                    logger.warning(
                        "[#%s] 第 %d 次 JSON 解析/校验失败，重试: %s",
                        article.id, attempt + 1, exc,
                    )
                    system = _retry_system_prompt(system, str(exc))
                    continue
                result.error = f"AI 输出解析失败: {exc}"
                logger.error("[#%s] %s", article.id, result.error)
                self._save_failure(article, result.error, result.model)
                return result

            self._save_analysis(article, analysis, result.model, result.usage)
            result.ok = True
            result.status = "success"
            return result

        return result

    def _save_analysis(
        self,
        article: Article,
        analysis: ArticleAnalysis,
        model: str,
        usage: dict,
    ) -> None:
        """持久化 analysis 到 article_analysis 表。"""
        provider_name = self.config.provider or "openai-compatible"
        self.storage.upsert_analysis(
            article_id=article.id or 0,
            provider=provider_name,
            model=model or self.config.model,
            prompt_version=PROMPT_VERSION,
            summary_zh=analysis.summary_zh,
            key_points=analysis.key_points,
            topics=analysis.topics,
            entities=analysis.entities,
            market_relevance=analysis.market_relevance,
            market_relevance_reason=analysis.market_relevance_reason,
            language=analysis.language,
            status="success",
            error="",
            usage=usage,
        )
        logger.info(
            "[#%s] AI 分析成功 → article_analysis (provider=%s model=%s pv=%s)",
            article.id, provider_name, model or self.config.model, PROMPT_VERSION,
        )

    def _save_failure(self, article: Article, error: str, model: str) -> None:
        """记录一次失败的 AI 分析（status='failed' + error），供 --retry-failed 使用。"""
        provider_name = self.config.provider or "openai-compatible"
        self.storage.upsert_analysis(
            article_id=article.id or 0,
            provider=provider_name,
            model=model or self.config.model,
            prompt_version=PROMPT_VERSION,
            summary_zh="",
            key_points=[],
            topics=[],
            entities=[],
            market_relevance="low",
            market_relevance_reason="",
            language="",
            status="failed",
            error=error,
            usage={},
        )
        logger.warning(
            "[#%s] AI 分析失败已记录 → article_analysis (provider=%s model=%s pv=%s)",
            article.id, provider_name, model or self.config.model, PROMPT_VERSION,
        )

    # ---------- 批量处理 ----------

    def process_batch(
        self,
        *,
        source_id: Optional[str] = None,
        limit: int = 5,
        article_id: Optional[int] = None,
        retry_failed: bool = False,
    ) -> BatchStats:
        """处理一批未分析的 Article。

        行为：
        1. 从 SQLite 取出需要处理的文章（未分析 或 retry_failed 标记的失败项）；
        2. 逐篇调用 AI 并入库；
        3. 单篇失败不影响其它文章。
        """
        stats = BatchStats()
        articles = self._pick_articles(
            source_id=source_id,
            limit=limit,
            article_id=article_id,
            retry_failed=retry_failed,
        )
        stats.total = len(articles)

        for article in articles:
            r = self.process_article(article)
            if r.status == "success":
                stats.ok += 1
            else:
                stats.failed += 1
                stats.errors.append(f"#{article.id} {article.title[:40]}: {r.error}")
        return stats

    def _pick_articles(
        self,
        *,
        source_id: Optional[str],
        limit: int,
        article_id: Optional[int],
        retry_failed: bool,
    ) -> list[Article]:
        """选择需要处理的文章。"""
        if article_id is not None:
            art = self.storage.get_article(article_id)
            if art is None:
                raise ValueError(f"文章不存在: article_id={article_id}")
            if self.storage.analysis_exists(article_id=article_id) and not retry_failed:
                logger.info("[#%s] 已有分析结果，跳过", article_id)
                return []
            return [art]

        arts = self.storage.list_unanalyzed_articles(
            source_id=source_id,
            limit=limit,
            include_failed=retry_failed,
        )
        if not arts:
            logger.info("没有待处理的文章（已全部完成分析）")
        return arts


def _retry_system_prompt(original: str, error_msg: str) -> str:
    """在重试时追加错误说明，让模型重新输出。"""
    return (
        original
        + "\n\n## 上一次输出不符合要求，请重新输出严格 JSON。\n"
        + f"错误原因: {error_msg}\n"
        + "不要输出 Markdown 代码块，不要输出解释文字，只输出 JSON 对象。"
    )
