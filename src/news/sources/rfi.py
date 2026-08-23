"""RFI（法广中文）Source Adapter。

RFI 数据源严格遵循 **RSS 第一优先级，官网及其他来源只是 RSS 的补充**
的核心原则。RfiAdapter 按以下优先级发现文章：

1. **RSS（最高优先级）**—— 先尝试官方 RSS（``https://www.rfi.fr/zh/rss``），
   再尝试 RSSHub 实例（``sites/rfi.yaml`` 的 ``rsshub_instances``）。
   RSS 能获取到的新闻，优先全部采用 RSS 数据。RSS 条目若带完整正文
   （``content_html``），直接入库，不再请求官网文章页；RSS 只有标题/摘要/URL
   时，保留该条目，后续只对这些需要正文的条目请求官网文章页。
2. **RFI 中文官网栏目页（补充来源）**—— 官网栏目 discovery 只能作为 RSS
   的补充。先完成 RSS discovery，再抓官网栏目。官网发现的文章必须首先与
   RSS 已发现的文章进行 URL / canonical URL / fingerprint 去重。RSS 已经
   存在的文章，官网发现后不得再次作为新文章处理。只有官网发现、而 RSS 没有
   覆盖的新闻，才进入补充集合。
3. **官网文章正文**—— RSS 已有完整正文 → 不访问官网正文页；RSS 没有正文，
   但有文章 URL → 可以访问官网正文页；官网栏目新增、RSS 没有覆盖的文章 →
   可以访问官网正文页。

数据流：

```
RSS discovery
  → RSS 去重
  → RSS 完整正文 → 直接入库
  → RSS 无正文 → 标记为需要补正文
  → 官网栏目 discovery
  → 与 RSS 已有文章去重
  → 只保留 RSS 没有的新文章
  → 对需要正文的文章进行官网正文抓取
  → 最终统一去重、质量检查、按发布时间排序
  → limit = 最终输出数量
```

RFI 特有的逻辑（官网栏目聚合、RSSHub 回退、HTML 正文选择器）全部集中在本模块，
不污染通用 discover / pipeline。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from selectolax.parser import HTMLParser

from ..discover import DiscoveredItem, discover_from_rss
from ..fetch import BaseFetcher
from ..model import Article
from ..normalize import canonicalize_url
from .base import SourceAdapter

logger = logging.getLogger(__name__)

# 不再注入 Chrome 风格浏览器 UA：实验确认 Chrome UA 会触发 RFI 等站点的 403。
# fetch_custom_headers() 返回 None，让 RFI article fetch 使用 httpx 默认 UA
# （python-httpx/...），避免 403。不要重新引入 Cookie / Referer / 浏览器伪装
# 头 / Playwright——这些不属于本次 RFI GUI 主线范围。

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

# RFI discovery 的硬性时间窗口（天）。只接受最近 ``RFI_DISCOVERY_MAX_AGE_DAYS``
# 天以内的 RFI 文章，防止栏目页中的“推荐阅读 / 相关文章 / 专题 / 历史内容”等
# 模块把历史文章带进候选池。这是 discovery candidate filter，不是 storage filter。
# 不允许因为凑 limit 而无限向历史文章扩展。
RFI_DISCOVERY_MAX_AGE_DAYS = 7


def _within_discovery_window(
    published_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """判断 RFI 文章发布日期是否在 discovery 时间窗口内（默认最近 7 天）。

    - 无发布时间（无法判断）→ 保留，避免误伤无法解析日期的正常候选；
    - ``published_at`` 超过窗口 → 返回 False，候选被过滤（不进 candidate pool、
      不触发 article fetch、也不计入 failed）；
    - 参考时间默认取当前 UTC 时间；测试可注入固定 ``now`` 保证确定性、避免时区 bug。
    """
    if published_at is None:
        return True
    reference = now if now is not None else datetime.now(timezone.utc)
    return published_at >= reference - timedelta(days=RFI_DISCOVERY_MAX_AGE_DAYS)

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
    html: str, page_url: str, *, now: Optional[datetime] = None
) -> list[DiscoveredItem]:
    """从 RFI 中文栏目页 HTML 提取文章链接列表。

    - 匹配 RFI 文章 URL 模式（含日期段 ``/cn/<分类>/<YYYYMMDD>-``）；
    - 用 ``canonicalize_url`` 去重；
    - 解析 ``published_at``：优先 JSON-LD 按 URL 精确关联，其次页面
      ``<time>`` 元素（尽力关联），最后回退到文章 URL 自带日期；
    - **discovery 时间窗口过滤**：超过 ``RFI_DISCOVERY_MAX_AGE_DAYS`` 天的
      历史文章直接跳过（不进 candidate pool），避免栏目页的“推荐阅读 /
      相关文章 / 专题 / 历史内容”模块把老文章带进候选池。

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
        # discovery 时间窗口过滤：超过最近 7 天的历史文章跳过，不进候选池。
        if not _within_discovery_window(published, now=now):
            continue
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

    def discover(
        self,
        *,
        fetcher: BaseFetcher,
        max_items: int,
        existing_urls: set[str] | None = None,
        now: Optional[datetime] = None,
    ) -> list[DiscoveredItem]:
        """发现 RFI 中文新闻（RSS 第一优先级，官网只是补充）。

        ``max_items`` 语义：**实际新增并成功入库的可读新闻数量**。
        discovery 层负责按来源优先级提供足够多的候选，跳过数据库中已
        存在的文章（``existing_urls``），持续从 RSS / RSSHub / 官网寻找
        直到收集到 max_items 个尚未入库的候选，或候选来源耗尽。

        数据流：
        1. 官方 RSS → 过滤已存在（existing_urls）→ 收集新候选
        2. RSSHub → 官方 RSS 不足时补充（同样过滤已存在）
        3. 官网栏目 → 仅当 RSS 层仍不足时补充（同样过滤已存在）
        4. 合并所有新候选，按发布时间排序，截断到 max_items

        ``existing_urls``：数据库中已存在的 canonical URL 集合。发现阶段
        跳过这些文章，不从 limit 名额中扣减——重复文章不消耗 limit。
        """
        # 数据库已有的 canonical URLs（用于跳过已入库文章）
        db_existing: set[str] = set(existing_urls or set())

        # ========== Step 1: RSS discovery（第一优先级）==========
        rss_items: list[DiscoveredItem] = []
        rss_seen: set[str] = set(db_existing)  # 防止跨来源重复

        # ---- 统计信息 ----
        rss_official_total = 0      # 官方 RSS 返回总数
        rss_official_dedup = 0      # 官方 RSS 内部去重后
        rss_official_in_db = 0      # 官方 RSS 已在数据库
        rss_official_new = 0        # 官方 RSS 新候选
        rsshub_total = 0            # RSSHub 返回总数
        rsshub_in_db = 0            # RSSHub 已在数据库
        rsshub_new = 0              # RSSHub 新候选

        # ---- 1a. 官方 RSS ----
        try:
            official_items = discover_from_rss(OFFICIAL_RSS, fetcher=fetcher)
            if official_items:
                rss_official_total = len(official_items)
                # 先按 canonical URL 去重（RSS 内部重复）
                seen_local: set[str] = set()
                for it in official_items:
                    canon = canonicalize_url(it.url)
                    if not canon or canon in seen_local:
                        continue
                    seen_local.add(canon)
                    rss_official_dedup += 1
                    if canon in db_existing:
                        rss_official_in_db += 1
                        continue  # 数据库已存在 → 跳过，不消耗 limit
                    rss_seen.add(canon)
                    rss_items.append(it)
                    rss_official_new += 1
                logger.info(
                    "[RFI] RSS 官方：返回 %d，去重 %d，已存在 %d，新候选 %d",
                    rss_official_total,
                    rss_official_dedup,
                    rss_official_in_db,
                    rss_official_new,
                )
        except Exception as exc:
            logger.warning(
                "[RFI] RSS 官方不可用 → 启用官网补充模式（失败: %s）",
                exc,
            )

        # ---- 1b. RSSHub（官方 RSS 失败或 RSS 新候选不足时尝试）----
        # 检查 RSS 层新候选是否已达 max_items（rss_items 中不含已在库的 URL）
        rss_new_count = len(rss_items)
        if rss_new_count < max_items:
            for instance in self.rsshub_instances:
                # 已有 max_items 个新候选 → 停止
                if len(rss_items) >= max_items:
                    break
                try:
                    hub_items = discover_from_rss(instance, fetcher=fetcher)
                    if hub_items:
                        hub_total = len(hub_items)
                        rsshub_total += hub_total
                        hub_seen: set[str] = set()
                        hub_added = 0
                        hub_in_db = 0
                        for it in hub_items:
                            canon = canonicalize_url(it.url)
                            if not canon or canon in hub_seen:
                                continue
                            hub_seen.add(canon)
                            if canon in db_existing or canon in rss_seen:
                                if canon in db_existing:
                                    hub_in_db += 1
                                continue  # 已存在 → 跳过
                            rss_seen.add(canon)
                            rss_items.append(it)
                            hub_added += 1
                            rsshub_new += 1
                            # 已收集到足够 → 停止该实例
                            if len(rss_items) >= max_items:
                                break
                        logger.info(
                            "[RFI] RSSHub %s：返回 %d，已存在 %d，新增 %d（累计 RSS 新候选 %d）",
                            instance,
                            hub_total,
                            hub_in_db,
                            hub_added,
                            rss_new_count + rsshub_new,
                        )
                        if hub_added == 0:
                            break  # 无新增 → 不再尝试更多实例
                except Exception as exc:
                    logger.warning("[RFI] RSSHub 实例 %s 失败: %s", instance, exc)

        rss_final_new = len(rss_items)
        logger.info(
            "[RFI] RSS 层：新候选 %d（官方 %d + RSSHub %d），limit=%d",
            rss_final_new,
            rss_official_new,
            rsshub_new,
            max_items,
        )

        # ========== Step 2: 官网栏目补充（仅当 RSS 层不足时）==========
        new_from_official: list[DiscoveredItem] = []
        if rss_final_new < max_items:
            # 记录启用官网补充的原因
            logger.info(
                "[RFI] RSS 新候选 %d < limit %d，启用官网补充",
                rss_final_new,
                max_items,
            )

            official_categories_items = self._discover_official_categories(
                fetcher=fetcher,
                existing_urls=rss_seen | db_existing,
                max_items=max_items - rss_final_new,
                now=now,
            )

            # 官网新增（RSS 未覆盖且数据库不存在）的条目
            for it in official_categories_items:
                canon = canonicalize_url(it.url)
                if not canon or canon in rss_seen:
                    continue
                rss_seen.add(canon)
                new_from_official.append(it)

            logger.info(
                "[RFI] 官网补充：RSS 已提供 %d，官网新增 %d",
                rss_final_new,
                len(new_from_official),
            )
        else:
            logger.info(
                "[RFI] RSS 新候选 %d >= limit %d，跳过官网补充",
                rss_final_new,
                max_items,
            )

        # ========== Step 3: 合并 RSS + 官网补充，统一排序 ==========
        all_items = rss_items + new_from_official

        # 按发布时间从新到旧排序；无发布时间的条目排到末尾（保持相对顺序）。
        with_time = [it for it in all_items if it.published_at is not None]
        without_time = [it for it in all_items if it.published_at is None]
        with_time.sort(key=lambda it: it.published_at, reverse=True)
        sorted_items = with_time + without_time

        # 返回所有过滤后的新候选（不截断到 max_items）。
        # max_items 是目标 usable 数，不是候选数。
        # 即使候选超过 max_items，也全部返回，由 pipeline 持续消费
        # 直到 usable_count >= max_items 或候选耗尽（正文失败不消耗 limit）。
        result = sorted_items
        if len(result) < max_items:
            logger.info(
                "[RFI] 所有来源已耗尽：最终只找到 %d 篇新候选（limit=%d）",
                len(result), max_items,
            )
        else:
            logger.info(
                "[RFI] 最终输出 %d 篇候选（RSS 新候选 %d + 官网补充 %d，limit=%d）",
                len(result),
                rss_final_new,
                len(new_from_official),
                max_items,
            )
        return result

    def _discover_official_categories(
        self,
        *,
        fetcher: BaseFetcher,
        existing_urls: set[str] | None = None,
        max_items: int = 100,
        now: Optional[datetime] = None,
    ) -> list[DiscoveredItem]:
        """从 RFI 中文官网栏目页聚合发现 RSS 未覆盖的文章。

        依次抓取 ``RFI_CN_CATEGORY_PAGES`` 各栏目页，从每个栏目页提取文章
        链接并用 canonical URL 去重；单个栏目失败只记 warning 并继续下一个。
        当收集到 ``max_items`` 个新文章后停止继续抓取后续栏目页。

        ``existing_urls``：RSS 已发现的 canonical URL 集合（含数据库已有 URL），
        用于在官网发现时跳过这些已有文章（RSS 已经存在的文章，官网发现后
        不得再次作为新文章）。

        ``max_items``：本次最多返回多少篇新文章。达到后立即停止，避免为
        凑数量去抓取大量栏目页（降低 RFI 反爬风险）。
        """
        collected: list[DiscoveredItem] = []
        seen: set[str] = set(existing_urls or set())

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
            # 已收集到足够多的新文章 → 停止
            if len(collected) >= max_items:
                break
            try:
                html = fetcher.fetch(cat_url)
                cat_items = _extract_articles_from_category_page(html, cat_url, now=now)
                added = _add(cat_items)
                logger.info(
                    "[RFI] 官网栏目%s：发现 %d，新增 %d，累计 %d",
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
            "[RFI] 官网栏目聚合：新文章 %d 条（limit=%d）",
            len(collected),
            max_items,
        )

        # 按发布时间从新到旧排序；无发布时间的条目排到末尾（保持相对顺序）。
        with_time = [it for it in collected if it.published_at is not None]
        without_time = [it for it in collected if it.published_at is None]
        with_time.sort(key=lambda it: it.published_at, reverse=True)
        sorted_items = with_time + without_time

        return sorted_items[:max_items]

    def fetch_custom_headers(self) -> Optional[dict[str, str]]:
        # 返回 None，让 RFI article fetch 使用 httpx 默认 UA。
        # 实验确认 Chrome 风格 UA 会触发 RFI 等站点的 403。
        # 不要重新设置 Chrome UA / 浏览器头 / Cookie，也不要引入 Playwright。
        # discovery 仍走现有 fetch()，article fetch 仍走现有 fetch_article()。
        return None

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
