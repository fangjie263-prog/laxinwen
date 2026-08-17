"""RFI（法广中文）Source Adapter。

法广（Radio France Internationale）中文站点通过 RSS 发布新闻。RSS 源在部分网络
环境下不可直接访问，因此 RfiAdapter 按以下优先级发现文章：

1. **官方 RSS**（``https://www.rfi.fr/zh/rss``）—— 若可达且带完整
   ``content:encoded`` 正文，则 ``content_html`` 为完整正文，pipeline 走
   "discovery content short-circuit"（见 ``discover.has_usable_content``），
   不再 fetch 原文 URL；
2. **RSSHub 多实例 feed-level fallback** —— 当官方 RSS 不可达（如本环境）时，
   逐个尝试 ``sites/rfi.yaml`` 的 ``rsshub_instances``（Phase 3 实际验证的两个
   实例），一个实例整个失败才切下一个，不对每篇文章重复请求。RSSHub 的
   summary 通常携带完整正文 HTML（含 ``.t-content__chapo`` 导语 + 多段正文 +
   ``.t-content__main-media`` 图片区），因此 ``discover._resolve_content_html``
   会把它作为 ``content_html`` → ``has_usable_content`` 为 True → pipeline 走
   0-fetch 短路；
3. **HTML fallback** —— 仅当 content 与 summary 都不可用时，pipeline 下载原文
   HTML，由 :meth:`RfiAdapter.extract_article` 用 RFI 页面结构
   （``.t-content__chapo`` 导语 + ``.t-content__body`` 正文容器）提取完整正文。

RFI 特有的逻辑（RSS/RSSHub 回退、HTML 正文选择器）全部集中在本模块，
不污染通用 discover / pipeline。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from selectolax.parser import HTMLParser

from ..discover import DiscoveredItem, discover_from_rss
from ..fetch import BaseFetcher
from ..model import Article
from ..normalize import canonicalize_url
from .base import SourceAdapter

logger = logging.getLogger(__name__)

# 桌面 Chrome UA（RSSHub / RFI 均对非浏览器 UA 更友好）
RFI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 官方 RSS（法广中文）
OFFICIAL_RSS = "https://www.rfi.fr/zh/rss"

# RSSHub RFI 中文 route —— Phase 3 实际验证可用的实例（供未配置时兜底）。
# 实际生效的实例列表来自 ``sites/rfi.yaml`` 的 ``rsshub_instances``；
# 这里作为代码级默认，与配置保持一致。
RSSHUB_RFI_CN = "https://rsshub.rssforever.com/rfi/cn"
RSSHUB_RFI_CN_BACKUP = "https://rsshub.ktachibana.party/rfi/cn"
_DEFAULT_RSSHUB_INSTANCES = (RSSHUB_RFI_CN, RSSHUB_RFI_CN_BACKUP)

# RFI 中文分类（RSSHub route ``/rfi/cn/<slug>``）—— 首页之后依次请求，跨分类
# 用 canonical URL 去重，达到 max_items 即停止。列表含 Phase 3 调查确认有效的
# 主要分类。
RFI_CN_CATEGORIES: tuple[str, ...] = (
    "politique",       # 政治
    "moyen-orient",    # 中东
    "international",   # 国际
    "taiwan",          # 港澳台
    "societe",         # 社会
    "economie",        # 经济
    "sports",          # 体育
    "afrique",         # 非洲
    "asie",            # 亚洲
    "europe",          # 欧洲
    "chine",           # 中国
)


def extract_title(html: str) -> str:
    """提取 RFI 文章标题，优先级：``<h1>`` → ``og:title`` → ``<title>``。

    与 HKEJ 的 fallback 思路一致：RFI 页面 ``<h1>`` 为干净标题，
    ``og:title`` 次之，``<title>`` 可能带站点后缀需剥离。
    """
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if text:
            return text
    m = re.search(r'property="og:title".*?content="(.*?)"', html, re.DOTALL)
    if m:
        text = m.group(1).strip()
        if text:
            return text
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if m:
        text = m.group(1).strip()
        for sep in (" - ", "|", "–"):
            if sep in text:
                text = text.split(sep)[0].strip()
        return text if text else ""
    return ""


def _container_html(node) -> str:
    """把 selectolax 节点序列化为 HTML 字符串。"""
    return node.html if node.html else ""


def extract_body_from_html(html: str) -> tuple[str, str]:
    """从 RFI 原文 HTML 提取正文，返回 ``(body_html, body_text)``。

    结构：
    - ``.t-content__chapo`` —— 导语段（文章开头）；
    - ``.t-content__body``  —— 正文容器。

    两者都取，导语放在正文之前；提取不到正文容器时返回 ``("", "")``，
    由调用方回退到通用提取。``body_html`` 保留原 HTML 片段，``body_text``
    为去标签后的纯文本。
    """
    try:
        tree = HTMLParser(html)
    except Exception:
        return "", ""
    body_node = tree.css_first(".t-content__body")
    if body_node is None:
        return "", ""
    body_html = _container_html(body_node)
    body_text = (body_node.text() or "").strip()

    chapo = tree.css_first(".t-content__chapo")
    if chapo is not None:
        chapo_html = _container_html(chapo)
        chapo_text = (chapo.text() or "").strip()
        body_html = chapo_html + body_html
        body_text = (chapo_text + "\n" + body_text).strip()
    return body_html, body_text


class RfiAdapter(SourceAdapter):
    """RFI（法广中文）Source Adapter。

    只负责发现新闻（官方 RSS → RSSHub 多实例 feed-level fallback）与
    HTML fallback 正文提取；去重 / 入库交给 laxinwen 通用 pipeline。
    """

    def __init__(self, source_id: str, source_name: str, site_cfg: dict | None = None) -> None:
        super().__init__(source_id, source_name)
        # RSSHub 实例列表：优先取站点配置 ``rsshub_instances``（Phase 3 验证的两个
        # 实例），否则回退到代码级默认。
        instances = (site_cfg or {}).get("rsshub_instances") or _DEFAULT_RSSHUB_INSTANCES
        self.rsshub_instances: list[str] = list(instances)

    def discover(self, *, fetcher: BaseFetcher, max_items: int) -> list[DiscoveredItem]:
        """发现 RFI 中文新闻，优先级：官方 RSS → RSSHub 多实例 feed-level fallback。

        返回 ``DiscoveredItem``；若 RSS 带完整正文，``content_html`` 非空，
        pipeline 走 short-circuit；否则为 None，pipeline 走 HTML fallback。
        """
        # 1. 官方 RSS
        try:
            items = discover_from_rss(OFFICIAL_RSS, fetcher=fetcher)
            if items:
                logger.info("[rfi] 官方 RSS 返回 %d 条", len(items))
                return items[:max_items]
        except Exception as exc:
            logger.warning("[rfi] 官方 RSS 失败: %s", exc)

        # 2. RSSHub 多实例 feed-level fallback：逐个实例尝试，一个实例整个失败
        #    才切下一个（不要把两个 instance 合并）。每个实例内先请求首页，再
        #    依次请求 RFI 中文分类页面，跨分类用 canonical URL 去重，达到
        #    max_items 立即停止。
        last_exc: Exception | None = None
        for instance in self.rsshub_instances:
            logger.info("[rfi] 尝试 RSSHub 实例: %s", instance)
            try:
                items = self._discover_rsshub_instance(
                    fetcher=fetcher, max_items=max_items, instance=instance
                )
                if items:
                    logger.info(
                        "[rfi] RSSHub 实例 %s 聚合返回 %d 条", instance, len(items)
                    )
                    return items
                logger.warning("[rfi] RSSHub 实例 %s 无条目", instance)
            except Exception as exc:
                last_exc = exc
                logger.warning("[rfi] RSSHub 实例 %s 失败: %s", instance, exc)

        logger.warning(
            "[rfi] 官方 RSS 与所有 RSSHub 实例均失败，无候选文章"
            + (f"（最近错误: {last_exc}" if last_exc else "")
        )
        return []

    def _discover_rsshub_instance(
        self, *, fetcher: BaseFetcher, max_items: int, instance: str
    ) -> list[DiscoveredItem]:
        """在单个 RSSHub 实例内聚合发现：首页 + RFI 中文分类页面。

        依次请求首页（``<instance>``）与 ``RFI_CN_CATEGORIES`` 各分类页面
        （``<instance>/<slug>``），跨分类用 canonical URL 去重；达到 ``max_items``
        立即停止，不再请求后续 feed。单个 feed 失败只记 warning 并继续下一个，
        不中断整个实例。返回聚合去重后的候选列表（最多 ``max_items`` 条）。
        """
        collected: list[DiscoveredItem] = []
        seen: set[str] = set()

        def _add(feed_items: list[DiscoveredItem]) -> int:
            added = 0
            for it in feed_items:
                canon = canonicalize_url(it.url)
                if not canon or canon in seen:
                    continue
                seen.add(canon)
                collected.append(it)
                added += 1
            return added

        # 1. 首页
        try:
            home_items = discover_from_rss(instance, fetcher=fetcher)
            added = _add(home_items)
            logger.info(
                "[rfi] RSSHub 实例 %s 首页返回 %d 条（新增 %d，累计 %d）",
                instance,
                len(home_items),
                added,
                len(collected),
            )
            if len(collected) >= max_items:
                return collected[:max_items]
        except Exception as exc:
            logger.warning("[rfi] RSSHub 实例 %s 首页失败: %s", instance, exc)

        # 2. 分类页面（达到 max_items 立即停止）
        for slug in RFI_CN_CATEGORIES:
            if len(collected) >= max_items:
                break
            category_url = f"{instance}/{slug}"
            try:
                cat_items = discover_from_rss(category_url, fetcher=fetcher)
                added = _add(cat_items)
                logger.info(
                    "[rfi] RSSHub 实例 %s 分类 %s 返回 %d 条（新增 %d，累计 %d）",
                    instance,
                    slug,
                    len(cat_items),
                    added,
                    len(collected),
                )
            except Exception as exc:
                logger.warning(
                    "[rfi] RSSHub 实例 %s 分类 %s 失败: %s", instance, slug, exc
                )

        return collected[:max_items]

    def fetch_custom_headers(self) -> Optional[dict[str, str]]:
        # RFI / RSSHub 用浏览器 UA + 中文语言更友好
        return {
            "User-Agent": RFI_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def extract_article(self, article: Article, html: str, url: str = "") -> bool:
        """RFI HTML fallback：从原文 HTML 提取完整正文并回填 ``article``。

        使用 RFI 页面结构 ``.t-content__chapo`` + ``.t-content__body``，
        同时设置 ``body_html`` 与 ``body_text``。

        返回 True 表示已成功提取正文；返回 False 表示未提取到正文容器，
        由 pipeline 回退到通用提取。
        """
        body_html, body_text = extract_body_from_html(html)
        if not body_html and not body_text:
            logger.info("[rfi] HTML fallback 未找到正文容器，回退通用提取")
            return False
        title = extract_title(html)
        if title:
            article.title = title
        article.body_html = body_html
        article.body_text = body_text
        return True
