"""RFI（法广中文）Source Adapter。

法广（Radio France Internationale）中文站点通过 RSS 发布新闻。RSS 源在部分网络
环境下不可直接访问，因此 RfiAdapter 按以下优先级发现文章：

1. **官方 RSS**（``https://www.rfi.fr/zh/rss``）—— 若可达且带完整
   ``content:encoded`` 正文，则 ``content_html`` 为完整正文，pipeline 走
   "discovery content short-circuit"（见 ``discover.has_usable_content``），
   不再 fetch 原文 URL；
2. **RSSHub**（``https://rsshub.rssforever.com/rfi/cn``）—— 当官方 RSS
   不可达（如本环境）时回退。RSSHub 只返回导语（``t-content__chapo``）与媒体，
   不含完整正文，因此 ``content_html`` 为空 → ``has_usable_content`` 为 False →
   pipeline 触发原文 URL 的 HTML fallback；
3. **HTML fallback** —— pipeline 下载原文 HTML，由
   :meth:`RfiAdapter.extract_article` 用 RFI 页面结构
   （``.t-content__chapo`` 导语 + ``.t-content__body`` 正文容器）提取完整正文，
   同时回填 ``body_html`` 与 ``body_text``。

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
from .base import SourceAdapter

logger = logging.getLogger(__name__)

# 桌面 Chrome UA（RSSHub / RFI 均对非浏览器 UA 更友好）
RFI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 官方 RSS（法广中文）
OFFICIAL_RSS = "https://www.rfi.fr/zh/rss"

# RSSHub RFI 中文 route（当前网络环境下官方 RSS 不可达，用 RSSHub 回退）
RSSHUB_RFI_CN = "https://rsshub.rssforever.com/rfi/cn"


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

    只负责发现新闻（官方 RSS → RSSHub 回退）与 HTML fallback 正文提取；
    去重 / 入库交给 laxinwen 通用 pipeline。
    """

    def __init__(self, source_id: str, source_name: str) -> None:
        super().__init__(source_id, source_name)

    def discover(self, *, fetcher: BaseFetcher, max_items: int) -> list[DiscoveredItem]:
        """发现 RFI 中文新闻，优先级：官方 RSS → RSSHub。

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

        # 2. RSSHub 回退
        try:
            items = discover_from_rss(RSSHUB_RFI_CN, fetcher=fetcher)
            if items:
                logger.info("[rfi] RSSHub 返回 %d 条", len(items))
                return items[:max_items]
        except Exception as exc:
            logger.warning("[rfi] RSSHub 失败: %s", exc)

        logger.warning("[rfi] 官方 RSS 与 RSSHub 均失败，无候选文章")
        return []

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
