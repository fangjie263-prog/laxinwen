"""新闻发现机制。

优先级：
1. 网站官方 RSS / Atom（feedparser）
2. RSSHub（feedparser 解析同一格式）
3. 网站公开栏目页（selectolax 提取文章链接）
4. “加载更多”分页接口（如 ECO admin-ajax load-more，用于批量补齐最近 N 篇）
5. 站内搜索（第一阶段不实现）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import feedparser
from selectolax.parser import HTMLParser

from .fetch import BaseFetcher
from .model import Article
from .normalize import canonicalize_url

logger = logging.getLogger(__name__)

# discovery content short-circuit 阈值：content_html 纯文本长度低于该值
# 视为“导语/摘要”而非“完整正文”，必须走原文 URL 的 HTML fallback。
# 阈值不宜过高（20 字即可区分“一句话导语”与真正的正文片段），否则会把
# 短但有效的 RSS 正文误判为摘要，导致不必要的官网正文请求。
_MIN_USABLE_CONTENT_CHARS = 20

# 单段落但作为完整正文时的最低纯文本长度。
# 摘要/导语通常是 1 个短段落（一句话），真正的正文即使只有 1 个段落也会
# 较长（> 80 字）。用于区分“只有摘要”与“带正文（即使短）”。
_MIN_SINGLE_PARAGRAPH_BODY_CHARS = 80


# HTML → text 时移除的“非正文”媒体/图片区域元素。
# RFI RSSHub summary 里 .t-content__main-media 等图片区含 <figure>/<figcaption>
# （图片版权说明，如 “REUTERS - Benoit Tessier”），不应进入 body_text。
_MEDIA_CLEAN_SELECTORS = (
    ".t-content__main-media",  # RFI 主图区
    "figure",                  # 图片块（含 img + figcaption）
    "figcaption",              # 图片说明/版权
    "img",                     # 内嵌图片
    "source",                  # picture 的 source
    "picture",
    "script",
    "style",
)


def html_to_text(html: str) -> str:
    """把 HTML 片段转为纯文本（去标签、折叠空白）。

    先移除媒体/图片区域（``figure`` / ``figcaption`` / ``.t-content__main-media``
    / ``img`` 等），避免 RFI 等站点的图片版权说明、图片 alt 文本进入正文；
    再取剩余文本并折叠空白。
    """
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        for node in tree.css(",".join(_MEDIA_CLEAN_SELECTORS)):
            node.decompose()
        text = (tree.body.text() if tree.body else tree.text() or "") or ""
    except Exception:
        # 兜底：解析失败时退回简单去标签
        text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", text).strip()


def has_usable_content(content_html: Optional[str]) -> bool:
    """判断 RSS 条目自带的 ``content_html`` 是否可作为完整正文。

    discovery content short-circuit 依据：

    - ``content_html`` 为 None / 空 → False（无正文）；
    - 纯文本长度 >= ``_MIN_USABLE_CONTENT_CHARS``（20 字）→ True。
      20 字足以区分"一句话导语"（< 20 字）与"正文片段"（>= 20 字）；
      但 content:encoded 的完整正文即使只有 1 个段落且较短，只要 >= 20 字
      就应被接受为正文（RSS 优先原则，不因短而丢弃）；
    - 纯文本长度 < 20 字 → False（空壳/失败占位/极短导语）。

    返回 True 时 pipeline 跳过 ``fetcher.fetch()`` 与 ``extract()``，直接用
    该 content_html 作为正文；返回 False 时必须 fetch 原文 URL + extract。
    摘要与完整正文的区分由 ``_resolve_content_html`` / ``_summary_is_usable_body``
    完成：content:encoded 直接视为正文，summary 需要多段落或足够长才视为正文。
    """
    if not content_html:
        return False
    return len(html_to_text(content_html)) >= _MIN_USABLE_CONTENT_CHARS


@dataclass
class DiscoveredItem:
    """发现到的文章条目（未下载正文）。"""

    url: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    summary: str = ""
    content_html: Optional[str] = None
    image: Optional[str] = None

    def to_article(self, source_id: str, source_name: str, language: str = "") -> Article:
        return Article(
            source_id=source_id,
            source_name=source_name,
            canonical_url=canonicalize_url(self.url),
            title=(self.title or "").strip(),
            authors=self.authors,
            published_at=self.published_at,
            body_text="",
            body_html=self.content_html,
            lead_image=self.image,
            language=language,
            status="new",
        )


def _parse_datetime(value: Optional[str], struct: Optional[object] = None) -> Optional[datetime]:
    """将 feedparser 的日期字段解析为带 UTC 时区的 datetime。

    优先使用 feedparser 已解析好的 time.struct_time（published_parsed/updated_parsed），
    它带 UTC 语义；其次尝试宽松解析原始字符串。
    """
    if struct is not None:
        try:
            dt = datetime(*struct[:6], tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            pass
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        from dateutil import parser as dateparser

        dt = dateparser.parse(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


def _resolve_content_html(entry) -> Optional[str]:
    """解析 RSS 条目的 content_html（正文 HTML）。

    优先级（保持 content/content:encoded 优先）：

    1. 若条目带 ``content`` / ``content:encoded``（feedparser 归一为 ``content``），
       取其 HTML 作为 ``content_html``（content:encoded 即完整正文，即使较短
       也保留，由 ``has_usable_content`` 判断是否可走 short-circuit）；
    2. 若没有 content，且 ``summary`` 含足够长的 HTML 正文（RFI 等 RSSHub
       summary 可能直接携带完整正文），则把 summary HTML 作为 ``content_html``；
       summary 需要满足多段落或足够长（避免单段落导语被误判为完整正文）；
    3. 否则返回 None，由 pipeline 触发原文 URL 的 HTML fallback。
    """
    if entry.get("content"):
        return entry["content"][0].get("value")
    summary_html = entry.get("summary", "") or ""
    if summary_html and _summary_is_usable_body(summary_html):
        return summary_html
    return None


def _summary_is_usable_body(summary_html: str) -> bool:
    """判断 RSS summary 是否包含完整正文（而非仅导语/摘要）。

    导语/摘要是 1 个短段落（一句话）；完整正文即使较短，也包含至少 2 个
    段落或 1 个较长段落（>= 80 字）。
    """
    text = html_to_text(summary_html)
    if len(text) < _MIN_USABLE_CONTENT_CHARS:
        return False
    paragraphs = len(re.findall(r"<p[^>]*>", summary_html, re.IGNORECASE))
    if paragraphs >= 2:
        return True
    return len(text) >= _MIN_SINGLE_PARAGRAPH_BODY_CHARS


def discover_from_rss(feed_url: str, *, fetcher: BaseFetcher | None = None) -> list[DiscoveredItem]:
    """从 RSS / Atom 解析文章条目（官方 RSS 或 RSSHub 通用）。

    - 若传入 fetcher：用 httpx 下载（带超时/重试/UA/节流），feedparser 只做解析；
    - 否则：feedparser 直接解析（默认 urllib）。
    """
    if fetcher is not None:
        try:
            raw = fetcher.fetch(feed_url)
        except Exception as exc:
            raise ValueError(f"RSS 下载失败: {feed_url} ({exc})") from exc
        parsed = feedparser.parse(raw)
    else:
        parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败: {feed_url} ({getattr(parsed, 'bozo_exception', '')})")
    items: list[DiscoveredItem] = []
    for e in parsed.entries:
        link = e.get("link", "")
        if not link:
            continue
        authors = []
        if e.get("authors"):
            authors = [a.get("name", "") for a in e["authors"] if a.get("name")]
        elif e.get("author"):
            authors = [e["author"]]
        image = None
        media = e.get("media_content") or e.get("media_thumbnail")
        if media:
            image = media[0].get("url")
        if not image:
            encl = e.get("enclosures") or []
            for enc in encl:
                if enc.get("type", "").startswith("image/"):
                    image = enc.get("href")
                    break
        items.append(
            DiscoveredItem(
                url=link,
                title=e.get("title", "").strip(),
                authors=authors,
                published_at=_parse_datetime(
                    e.get("published") or e.get("updated"),
                    e.get("published_parsed") or e.get("updated_parsed"),
                ),
                summary=re.sub(r"<[^>]+>", "", e.get("summary", "")).strip(),
                content_html=_resolve_content_html(e),
                image=image,
            )
        )
    return items


def discover_from_list_page(
    list_url: str,
    *,
    fetcher: BaseFetcher,
    link_selector: str,
    article_url_pattern: str | None = None,
    max_items: int = 50,
) -> list[DiscoveredItem]:
    """从栏目页解析文章链接（selectolax）。

    - link_selector：文章链接的 CSS selector（由站点配置提供）
    - article_url_pattern：可选，文章 URL 正则；用于过滤非文章链接
    """
    html = fetcher.fetch(list_url)
    tree = HTMLParser(html)
    pattern = re.compile(article_url_pattern) if article_url_pattern else None

    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    for node in tree.css(link_selector):
        href = node.attributes.get("href") or node.attributes.get("data-href") or ""
        if not href:
            continue
        # 解析相对路径
        if href.startswith("/"):
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(list_url)
            href = urlunsplit((parts.scheme, parts.netloc, href, "", ""))
        canon = canonicalize_url(href)
        if not canon or canon in seen:
            continue
        if pattern and not pattern.search(href):
            continue
        seen.add(canon)
        items.append(DiscoveredItem(url=canon))
        if len(items) >= max_items:
            break
    return items


# ---------- “加载更多”分页接口（ECO admin-ajax load-more） ----------


def _extract_json_object(text: str, var_name: str = "ECO_JS") -> Optional[dict]:
    """从 HTML 中提取 ``var_name = {...};`` 形式的 JS 对象字面量并解析为 dict。

    用于读取页面内嵌的 JS 配置（如 ECO 的 ``ECO_JS``，包含 load-more 所需的
    nonce / ajax url / 每页条数等）。解析失败返回 None。
    """
    m = re.search(rf"{re.escape(var_name)}\s*=\s*(\{{.*?\}})\s*;", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        logger.warning("%s JS 对象解析失败", var_name)
        return None


def discover_from_load_more(
    list_url: str,
    *,
    fetcher: BaseFetcher,
    load_more: dict,
    article_url_pattern: str | None = None,
    max_items: int = 50,
) -> list[DiscoveredItem]:
    """从“加载更多”分页接口批量发现文章（按 offset 翻页直到达到 max_items 或取不到更多）。

    ``load_more`` 站点配置示例（ECO）：

    .. code-block:: yaml

        load_more:
          endpoint_selector: "button.js-archive-load-more"   # 从按钮提取 data-action
          js_var: "ECO_JS"                                    # 内嵌 JS 配置变量名
          offset_param: "eco_offset"                          # offset 参数名
          action_param: "action"                              # action 参数名
          nonce_param: "nonce"                                # nonce 参数名
          nonce_key: "nonce_load_more"                        # ECO_JS 中 nonce 的 key
          url_key: "wp_ajax_url"                              # ECO_JS 中 ajax endpoint 的 key
          per_page_key: "archive_load_more"                   # ECO_JS 中每页条数的 key

    流程：
    1. 抓取栏目页 HTML；
    2. 从按钮 ``data-action`` 得到 action，从 JS 配置提取 ajax url / nonce / 每页条数；
    3. 以 ``offset`` 从栏目页初始文章数开始，逐页请求；
    4. 每页返回 ``posts_html``，用 selectolax 提取文章链接；
    5. 累计去重，直到达到 max_items 或连续空页 / 接口失败。

    返回文章列表。若无法提取配置（如页面结构变化），抛出 ValueError。
    """
    html = fetcher.fetch(list_url)
    tree = HTMLParser(html)
    pattern = re.compile(article_url_pattern) if article_url_pattern else None

    # --- 读取按钮 data-action / data-offset ---
    action = ""
    offset_hint: Optional[int] = None
    btn_selector = load_more.get("endpoint_selector", "")
    if btn_selector:
        for node in tree.css(btn_selector):
            action = node.attributes.get("data-action") or ""
            try:
                offset_hint = int(node.attributes.get("data-offset") or 0) or None
            except (TypeError, ValueError):
                offset_hint = None
            if action:
                break

    # --- 读取 JS 配置 ---
    js = _extract_json_object(html, load_more.get("js_var", "ECO_JS")) or {}
    ajax_url = js.get(load_more.get("url_key", "wp_ajax_url")) or load_more.get("endpoint")
    nonce = js.get(load_more.get("nonce_key", "nonce_load_more")) or load_more.get("nonce")
    per_page = js.get(load_more.get("per_page_key", "archive_load_more")) or load_more.get("per_page")

    if not ajax_url:
        raise ValueError(f"无法从 {list_url} 提取 load-more endpoint")
    if not action:
        raise ValueError(f"无法从 {list_url} 提取 load-more action（按钮选择器: {btn_selector or '(未配置)'}）")
    if not nonce:
        raise ValueError(f"无法从 {list_url} 提取 load-more nonce（JS 变量: {load_more.get('js_var', 'ECO_JS')}）")

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 12

    offset_param = load_more.get("offset_param", "eco_offset")
    action_param = load_more.get("action_param", "action")
    nonce_param = load_more.get("nonce_param", "nonce")

    # 初始文章：直接用首页 HTML 提取；offset 优先用按钮 data-offset（真实浏览器语义）
    initial_items = _parse_card_links(html, article_url_pattern=pattern)
    collected = _dedup_items(initial_items)
    offset = offset_hint if offset_hint is not None else len(collected)

    items: list[DiscoveredItem] = []
    empty_pages = 0
    while len(collected) < max_items:
        params = {
            action_param: action,
            offset_param: str(offset),
            nonce_param: nonce,
        }
        logger.debug("[load-more] %s %s", ajax_url, params)
        try:
            resp = fetcher.fetch(_url_with_query(ajax_url, params))
        except Exception as exc:
            logger.warning("[load-more] 第 %d 页请求失败: %s", offset, exc)
            break
        try:
            # 部分站点 JSON 带 UTF-8 BOM，先去 BOM
            data = json.loads(resp.lstrip("\ufeff"))
        except (ValueError, TypeError):
            logger.warning("[load-more] 第 %d 页响应不是 JSON，停止翻页", offset)
            break
        if not data.get("success"):
            logger.warning("[load-more] 第 %d 页 success=false（nonce 可能过期），停止翻页", offset)
            break
        payload = data.get("data") or {}
        posts_html = payload.get("posts_html") or ""
        page_items = _parse_card_links(posts_html, article_url_pattern=pattern)
        added = 0
        for it in page_items:
            if it.url not in {d.url for d in collected}:
                collected.append(it)
                added += 1
        logger.info("[load-more] offset=%d 新增 %d 条（累计 %d）", offset, added, len(collected))

        count = int(payload.get("count") or 0)
        if payload.get("last_batch") or count <= 0:
            break
        if added == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        offset += count if count > 0 else per_page

    items = collected[:max_items]
    return items


def _url_with_query(url: str, params: dict) -> str:
    """给 URL 附加 query 参数（保留已有 query）。"""
    from urllib.parse import urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = parts.query
    if query:
        query += "&" + urlencode(params)
    else:
        query = urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _parse_card_links(html: str, article_url_pattern: Optional[re.Pattern] = None) -> list[DiscoveredItem]:
    """从 load-more 返回的 posts_html（卡片列表）中提取文章链接。"""
    if not html:
        return []
    tree = HTMLParser(html)
    items: list[DiscoveredItem] = []
    seen: set[str] = set()
    # 优先所有 a[href] 中符合文章 URL 模式的链接，避免依赖具体卡片结构
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href:
            continue
        canon = canonicalize_url(href)
        if not canon or canon in seen:
            continue
        # 模式针对 canonical URL 匹配（去掉了 utm/fragment，避免带追踪参数的链接被误过滤）
        if article_url_pattern and not article_url_pattern.search(canon):
            continue
        seen.add(canon)
        items.append(DiscoveredItem(url=canon))
    return items


def _dedup_items(items: list[DiscoveredItem]) -> list[DiscoveredItem]:
    """按 canonical URL 去重并保持顺序。"""
    seen: set[str] = set()
    result: list[DiscoveredItem] = []
    for it in items:
        canon = canonicalize_url(it.url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        result.append(it)
    return result


def discover_for_site(
    cfg: dict,
    *,
    fetcher: BaseFetcher,
    max_items: int = 50,
    existing_urls: set[str] | None = None,
) -> list[DiscoveredItem]:
    """按站点配置执行发现流程。

    1. 若站点配置声明了 ``adapter``（如 HKEJ）→ 交给对应 SourceAdapter
       （复用 ResearchReader 的 HKEJ 抓取逻辑，见 ``sources/hkej.py``）；
    2. 否则继续使用通用发现流程（RSS → RSSHub → 栏目页 → load-more 补齐），
       ECO 等现有站点完全不受影响。

    通用发现与旧实现一致：多个来源**合并**而不是遇到一个就 return。
    目标是把候选文章凑到 ``max_items``（--limit 的语义：最近 N 篇的发现窗口）。
    各来源之间用 canonical URL 去重；load-more 仅在 RSS/栏目页不足时触发。
    """
    source_id = cfg.get("id", "")
    source_name = cfg.get("name", "")

    # Source Adapter 分发：声明了 adapter 的站点（如 HKEJ / RFI）直接走 adapter
    if cfg.get("adapter"):
        from .sources import get_adapter

        adapter = get_adapter(cfg)
        if adapter is not None:
            logger.info(
                "[%s] 使用 source adapter: %s", source_id, type(adapter).__name__
            )
            items = adapter.discover(
                fetcher=fetcher,
                max_items=max_items,
                existing_urls=existing_urls,
            )
            logger.info(
                "[%s] adapter 发现 %d 条候选（去重后）", source_id, len(items)
            )
            return items[:max_items]
        raise ValueError(
            f"站点 {source_id!r} 声明了 adapter={cfg.get('adapter')!r} 但无法加载"
        )

    collected: list[DiscoveredItem] = []
    seen: set[str] = set(existing_urls or set())

    def _add(items: list[DiscoveredItem]) -> int:
        """合并去重，返回新增条数。"""
        added = 0
        for it in items:
            canon = canonicalize_url(it.url)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            collected.append(it)
            added += 1
        return added

    # 1. 官方 RSS（最快，含正文）
    if cfg.get("rss"):
        logger.info("[%s] 尝试官方 RSS: %s", source_id, cfg["rss"])
        try:
            rss_items = discover_from_rss(cfg["rss"], fetcher=fetcher)
            _add(rss_items)
            logger.info("[%s] 官方 RSS 发现 %d 条", source_id, len(rss_items))
        except Exception as exc:
            logger.warning("[%s] 官方 RSS 失败: %s", source_id, exc)

    # 2. RSSHub
    if cfg.get("rsshub"):
        logger.info("[%s] 尝试 RSSHub: %s", source_id, cfg["rsshub"])
        try:
            rh_items = discover_from_rss(cfg["rsshub"], fetcher=fetcher)
            _add(rh_items)
            logger.info("[%s] RSSHub 发现 %d 条", source_id, len(rh_items))
        except Exception as exc:
            logger.warning("[%s] RSSHub 失败: %s", source_id, exc)

    # 3. 栏目页
    lists = cfg.get("lists") or []
    for lst in lists:
        url = lst.get("url", "")
        if not url:
            continue
        logger.info("[%s] 尝试栏目页: %s", source_id, url)
        try:
            list_items = discover_from_list_page(
                url,
                fetcher=fetcher,
                link_selector=lst.get("link_selector", ""),
                article_url_pattern=lst.get("article_url_pattern") or cfg.get("article_url_pattern"),
                max_items=max_items,
            )
            _add(list_items)
            logger.info("[%s] 栏目页发现 %d 条", source_id, len(list_items))
        except Exception as exc:
            logger.warning("[%s] 栏目页失败: %s", source_id, exc)

    # 4. load-more 分页接口（补齐最近 N 篇）
    if len(collected) < max_items and cfg.get("load_more"):
        load_more = cfg["load_more"]
        # 优先使用第一个 list 的 URL 作为入口页
        entry_url = (
            (cfg.get("lists") or [{}])[0].get("url")
            or load_more.get("list_url")
        )
        # 文章 URL 正则：优先取第一个 list 的（ECO 的 pattern 定义在 lists[0] 内）
        entry_pattern = (
            (cfg.get("lists") or [{}])[0].get("article_url_pattern")
            or cfg.get("article_url_pattern")
        )
        if entry_url:
            logger.info("[%s] 尝试 load-more 补齐: %s", source_id, entry_url)
            try:
                lm_items = discover_from_load_more(
                    entry_url,
                    fetcher=fetcher,
                    load_more=load_more,
                    article_url_pattern=entry_pattern,
                    max_items=max_items,
                )
                _add(lm_items)
                logger.info("[%s] load-more 累计发现 %d 条", source_id, len(lm_items))
            except Exception as exc:
                logger.warning("[%s] load-more 失败: %s", source_id, exc)

    logger.info("[%s] 合并发现 %d 条候选（去重后）", source_id, len(collected))
    return collected[:max_items]
