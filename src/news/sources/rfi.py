"""RFI（法广中文）Source Adapter。

法广（Radio France Internationale）中文站点的主要发现入口改为直接抓取
RFI 中文官网栏目页（而非 RSSHub /rfi/cn 作为主入口）。RfiAdapter 按以下
优先级发现文章：

1. **官方 RSS**（``https://www.rfi.fr/zh/rss``）—— 若可达且带完整
   ``content:encoded`` 正文，则 ``content_html`` 为完整正文，pipeline 走
   "discovery content short-circuit"（见 ``discover.has_usable_content``），
   不再 fetch 原文 URL；官方 RSS 在部分网络环境下返回 404，此时直接进入
   官网栏目方案；
2. **RFI 中文官网栏目页**（主入口）—— 依次抓取 ``RFI_CN_CATEGORY_PAGES``
   中列出的 RFI 中文栏目页（使用中文 slug，如 ``/cn/政治``、``/cn/中国``，
   不是 ``/rfi/cn/europe``、``/rfi/cn/chine`` 等错误 slug）。从每个栏目页
   HTML 中提取文章链接，用 ``canonicalize_url`` 去重；从页面 HTML / JSON-LD /
   文章 URL 中解析发布时间（RFI 文章 URL 本身包含日期，如
   ``/cn/中国/20260817-xxxxx``）。合并所有栏目候选后按 ``published_at``
   从新到旧排序，返回前 ``max_items`` 篇。若栏目页能直接提供完整正文则
   保留 ``content_html``（复用 0-fetch 短路），否则由 pipeline 后续 fetch
   文章正文；
3. **RSSHub 多实例 fallback** —— 仅当官网栏目页全部无法访问时，逐个尝试
   ``sites/rfi.yaml`` 的 ``rsshub_instances``（两个实例保持 fallback，不合并）。

RFI 特有的逻辑（官网栏目聚合、RSSHub 回退、HTML 正文选择器）全部集中在本模块，
不污染通用 discover / pipeline。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
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

# 官方 RSS（法广中文）—— 实践中常返回 404，404 后直接进入官网栏目方案
OFFICIAL_RSS = "https://www.rfi.fr/zh/rss"

# RSSHub RFI 中文 route —— 仅作为官网栏目全部失败时的 fallback。
# 实际生效的实例列表来自 ``sites/rfi.yaml`` 的 ``rsshub_instances``；
# 这里作为代码级默认，与配置保持一致。
RSSHUB_RFI_CN = "https://rsshub.rssforever.com/rfi/cn"
RSSHUB_RFI_CN_BACKUP = "https://rsshub.ktachibana.party/rfi/cn"
_DEFAULT_RSSHUB_INSTANCES = (RSSHUB_RFI_CN, RSSHUB_RFI_CN_BACKUP)

# RFI 中文官网栏目页（主入口）。使用已实际验证的中文 slug，绝不使用
# ``europe`` / ``chine`` 等英文 slug（已确认错误）。
# 每个元素为 ``(栏目名, 栏目 URL)``；首页 ``https://www.rfi.fr/cn/`` 放在最前。
RFI_CN_CATEGORY_PAGES: tuple[tuple[str, str], ...] = (
    ("首页", "https://www.rfi.fr/cn/"),
    ("政治", "https://www.rfi.fr/cn/政治"),
    ("中国", "https://www.rfi.fr/cn/中国"),
    ("国际", "https://www.rfi.fr/cn/国际"),
    ("法国", "https://www.rfi.fr/cn/法国"),
    ("欧洲", "https://www.rfi.fr/cn/欧洲"),
    ("亚洲", "https://www.rfi.fr/cn/亚洲"),
    ("中东", "https://www.rfi.fr/cn/中东"),
    ("非洲", "https://www.rfi.fr/cn/非洲"),
    ("港澳台", "https://www.rfi.fr/cn/港澳台"),
    ("社会", "https://www.rfi.fr/cn/社会"),
    ("体育", "https://www.rfi.fr/cn/体育"),
    ("专栏检索", "https://www.rfi.fr/cn/专栏检索"),
)

# 仅保留栏目名列表（供测试 / 兼容引用）：
# 依序对应 RFI_CN_CATEGORY_PAGES 中各栏目名。
RFI_CN_CATEGORY_NAMES: tuple[str, ...] = tuple(name for name, _ in RFI_CN_CATEGORY_PAGES)

# RFI 文章 URL 模式：``https://www.rfi.fr/cn/<分类>/<YYYYMMDD>-<slug>``。
# 匹配含日期段的文章链接，用于从栏目页筛选文章并解析日期。
_RFI_ARTICLE_URL_RE = re.compile(r"/cn/[^/\s]+/(\d{8})-")

# JSON-LD 中 ``datePublished`` 的键
_JSONLD_DATE_KEYS = ("datePublished", "dateModified")


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


def _parse_date_from_url(url: str) -> Optional[datetime]:
    """从 RFI 文章 URL 解析发布日期。

    RFI 文章 URL 自带日期，如 ``/cn/中国/20260817-xxxxx`` → 2026-08-17。
    返回带 UTC 时区的 datetime（时间为当天 00:00）。解析失败返回 None。
    """
    m = _RFI_ARTICLE_URL_RE.search(url)
    if not m:
        return None
    date_str = m.group(1)
    try:
        return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_jsonld_dates(html: str) -> list[tuple[str, str]]:
    """从页面 HTML 中提取 JSON-LD 的 ``(datePublished, url)`` 列表。

    扫描 ``<script type="application/ld+json">`` 块，解析其中的
    ``datePublished`` / ``dateModified`` 与对应的 ``url``（若存在），
    返回 ``(日期字符串, url)`` 列表。用于按文章 URL 精确关联发布时间。
    """
    dates: list[tuple[str, str]] = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # 可能是列表或单对象；尝试取 JSON 数组
            try:
                data = json.loads(raw)
            except Exception:
                continue
        # 归一化为列表
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            # 也可能是嵌套的 @graph
            if "@graph" in node and isinstance(node["@graph"], list):
                nodes.extend(n for n in node["@graph"] if isinstance(n, dict))
            url = node.get("url") or node.get("@id") or ""
            for key in _JSONLD_DATE_KEYS:
                val = node.get(key)
                if isinstance(val, str) and val:
                    dates.append((val, url or ""))
                    break
    return dates


def _extract_time_dates(html: str) -> list[str]:
    """从页面 HTML 中提取所有 ``<time datetime="...">`` 的日期字符串。"""
    dates: list[str] = []
    for m in re.finditer(
        r'<time[^>]*datetime=["\']([^"\']+)["\']', html, re.DOTALL
    ):
        if m.group(1):
            dates.append(m.group(1))
    return dates


def _parse_datetime_string(value: str) -> Optional[datetime]:
    """把 ISO 8601 日期字符串解析为带 UTC 时区的 datetime。

    兼容带时区（Z / +08:00）与不带时区的纯日期；不带时区视为 UTC。
    """
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # 纯 YYYYMMDD
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _absolute_url(base_url: str, href: str) -> str:
    """把相对 URL 解析为绝对 URL（基于栏目页 URL）。"""
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(base_url)
        return urlunsplit((parts.scheme, parts.netloc, href, "", ""))
    return href


def _extract_articles_from_category_page(
    html: str, page_url: str
) -> list[DiscoveredItem]:
    """从 RFI 中文栏目页 HTML 提取文章链接列表。

    - 匹配 RFI 文章 URL 模式（含日期段 ``/cn/<分类>/<YYYYMMDD>-``）；
    - 用 ``canonicalize_url`` 去重；
    - 解析 ``published_at``：优先 JSON-LD 按 URL 精确关联，其次页面
      ``<time>`` 元素（尽力关联），最后回退到文章 URL 自带日期。

    标题取自 ``<a>`` 文本；若无标题则留空（由 pipeline 后续 fetch 时补全）。
    """
    tree = HTMLParser(html)
    seen: set[str] = set()
    items: list[DiscoveredItem] = []
    link_nodes: list[tuple[str, str]] = []  # (abs_url, title)

    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        abs_url = _absolute_url(page_url, href)
        if not _RFI_ARTICLE_URL_RE.search(abs_url):
            continue
        canon = canonicalize_url(abs_url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        title = (node.text() or "").strip()
        link_nodes.append((canon, title))

    # 关联发布时间：JSON-LD 按 URL 精确匹配
    jsonld_dates = _extract_jsonld_dates(html)
    jsonld_map: dict[str, datetime] = {}
    for date_str, url in jsonld_dates:
        dt = _parse_datetime_string(date_str)
        if dt and url:
            canon = canonicalize_url(_absolute_url(page_url, url))
            if canon:
                jsonld_map.setdefault(canon, dt)

    for canon, title in link_nodes:
        published: Optional[datetime] = None
        # 1) JSON-LD 精确关联
        published = jsonld_map.get(canon)
        # 2) URL 自带日期
        if published is None:
            published = _parse_date_from_url(canon)
        items.append(
            DiscoveredItem(url=canon, title=title, published_at=published)
        )

    return items


class RfiAdapter(SourceAdapter):
    """RFI（法广中文）Source Adapter。

    发现流程：官方 RSS → RFI 中文官网栏目页（主入口）→ RSSHub 多实例
    fallback；HTML fallback 正文提取。
    """

    def __init__(self, source_id: str, source_name: str, site_cfg: dict | None = None) -> None:
        super().__init__(source_id, source_name)
        # RSSHub 实例列表：优先取站点配置 ``rsshub_instances``，否则回退到代码级默认。
        instances = (site_cfg or {}).get("rsshub_instances") or _DEFAULT_RSSHUB_INSTANCES
        self.rsshub_instances: list[str] = list(instances)

    def discover(self, *, fetcher: BaseFetcher, max_items: int) -> list[DiscoveredItem]:
        """发现 RFI 中文新闻。

        优先级：
        1. 官方 RSS（404 时直接进入官网栏目方案）；
        2. RFI 中文官网栏目页（主入口，聚合去重 + 按发布时间排序）；
        3. 官网栏目全部失败时，RSSHub 多实例 feed-level fallback（不合并）。

        返回 ``DiscoveredItem``；若 RSS / 栏目页带完整正文，``content_html``
        非空，pipeline 走 short-circuit；否则为 None，pipeline 走 HTML fallback。
        """
        # 1. 官方 RSS（实践中常 404，404 后直接进入官网栏目方案）
        try:
            items = discover_from_rss(OFFICIAL_RSS, fetcher=fetcher)
            if items:
                logger.info("[RFI] 官方 RSS 返回 %d 条", len(items))
                return items[:max_items]
        except Exception as exc:
            logger.info("[RFI] 官方 RSS 不可用: %s（进入官网栏目方案）", exc)

        # 2. RFI 中文官网栏目页（主入口）
        items = self._discover_official_categories(fetcher=fetcher, max_items=max_items)
        if items:
            logger.info(
                "[RFI] 官网栏目聚合：唯一文章 %d，按发布时间排序后取最近 %d 篇",
                len(items),
                min(len(items), max_items),
            )
            return items[:max_items]

        # 3. RSSHub 多实例 fallback：仅当官网栏目全部失败时。
        last_exc: Exception | None = None
        for instance in self.rsshub_instances:
            logger.info("[RFI] 尝试 RSSHub 实例（官网栏目不可用）: %s", instance)
            try:
                items = discover_from_rss(instance, fetcher=fetcher)
                if items:
                    logger.info(
                        "[RFI] RSSHub 实例 %s 返回 %d 条（官网栏目不可用 fallback）",
                        instance,
                        len(items),
                    )
                    return items[:max_items]
                logger.warning("[RFI] RSSHub 实例 %s 无条目", instance)
            except Exception as exc:
                last_exc = exc
                logger.warning("[RFI] RSSHub 实例 %s 失败: %s", instance, exc)

        logger.warning(
            "[RFI] 官方 RSS、官网栏目与所有 RSSHub 实例均失败，无候选文章"
            + (f"（最近错误: {last_exc}" if last_exc else "")
        )
        return []

    def _discover_official_categories(
        self, *, fetcher: BaseFetcher, max_items: int
    ) -> list[DiscoveredItem]:
        """从 RFI 中文官网栏目页聚合发现文章。

        依次抓取 ``RFI_CN_CATEGORY_PAGES`` 各栏目页，从每个栏目页提取文章
        链接并用 canonical URL 去重；单个栏目失败只记 warning 并继续下一个。
        所有栏目尝试完成后，按 ``published_at`` 从新到旧排序，返回前
        ``max_items`` 篇（不足则返回已有的全部，不凑数）。
        """
        collected: list[DiscoveredItem] = []
        seen: set[str] = set()

        def _add(items: list[DiscoveredItem]) -> int:
            added = 0
            for it in items:
                canon = canonicalize_url(it.url)
                if not canon or canon in seen:
                    continue
                seen.add(canon)
                collected.append(it)
                added += 1
            return added

        for cat_name, cat_url in RFI_CN_CATEGORY_PAGES:
            try:
                html = fetcher.fetch(cat_url)
                cat_items = _extract_articles_from_category_page(html, cat_url)
                added = _add(cat_items)
                logger.info(
                    "[RFI] 官网栏目%s：发现 %d，新增唯一 %d，累计 %d",
                    cat_name,
                    len(cat_items),
                    added,
                    len(collected),
                )
            except Exception as exc:
                logger.warning(
                    "[RFI] 官网栏目%s：失败 %s，累计仍为 %d",
                    cat_name,
                    exc,
                    len(collected),
                )

        logger.info(
            "[RFI] 官网栏目聚合：唯一文章 %d%s",
            len(collected),
            f" / limit {max_items}" if len(collected) < max_items else "",
        )

        # 按发布时间从新到旧排序；无发布时间的条目排到末尾（保持相对顺序）。
        with_time = [it for it in collected if it.published_at is not None]
        without_time = [it for it in collected if it.published_at is None]
        with_time.sort(key=lambda it: it.published_at, reverse=True)
        sorted_items = with_time + without_time

        logger.info(
            "[RFI] 按发布时间排序后取最近 %d 篇",
            min(len(sorted_items), max_items),
        )
        return sorted_items[:max_items]

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
            logger.info("[RFI] HTML fallback 未找到正文容器，回退通用提取")
            return False
        title = extract_title(html)
        if title:
            article.title = title
        article.body_html = body_html
        article.body_text = body_text
        return True
