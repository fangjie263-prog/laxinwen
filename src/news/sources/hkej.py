"""HKEJ（信報財經新聞）Source Adapter。

本模块的核心逻辑**直接迁移自 ResearchReader 项目已经实际验证过的
``work/hkej_scraper.py``**（https://github.com/fangjie263-prog/researchreader），
仅做两处适配：

1. 把"输出 JSON 文件"改为"输出 laxinwen 统一 ``DiscoveredItem``"，
   之后完全交给 laxinwen 现有 pipeline（去重 → 下载 → 正文提取 → SQLite）；
2. 内存 ``seen`` 去重只作为发现阶段去重（同一次抓取内避免重复 URL），
   最终跨运行去重完全依赖 laxinwen 的 SQLite 持久化去重。

从 ResearchReader 复用的 HKEJ-specific 成熟知识：

- ``LINK_RE``：HKEJ 即时新闻列表页文章链接正则
  ``(?<=href=")(/instantnews/[a-z]+/article/\\d+/[^"]*)(?=")``
  —— 已在 ResearchReader 真实使用验证（列表页每次约 40-80 条链接）；
- 列表页 URL / 分页逻辑：第 1 页 ``/instantnews``，
  第 2 页起 ``/instantnews/index?page=N``（ResearchReader 逐页抓取验证）；
- 标题 fallback：``<h1>`` → ``og:title`` → ``<title>``
  （ResearchReader ``_extract_title`` 验证的优先级；真实页面 h1 是干净标题，
  title 带 "信報網站 hkej.com" 等站点后缀需剥离）；
- 正文：``article-content`` 容器（ResearchReader 在真实文章中验证，
  提取正文时需跳过 script/style 并清理标签，正文干净无导航/广告）。

不搬入 ResearchReader 的其他部分（GUI / EPUB / PDF / AI / HTML / 数据库 /
配置系统 / 其他无关工具），HKEJ 抓取能力只以本 adapter 形式存在。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser as _PyHTMLParser
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

from selectolax.parser import HTMLParser

from ..discover import DiscoveredItem
from ..fetch import BaseFetcher
from ..normalize import canonicalize_url
from .base import SourceAdapter

logger = logging.getLogger(__name__)

# 真实研究结论：ResearchReader 保存的 hkej.com 列表页抓包显示，
# HKEJ 对非浏览器 UA 会拒绝访问（403/空白）。使用桌面 Chrome UA。
HKEJ_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BASE = "https://www.hkej.com"

# 列表页 URL：第 1 页 /instantnews，第 2 页起 /instantnews/index?page=N
LIST_URL = f"{BASE}/instantnews"

def _list_url(page: int) -> str:
    return LIST_URL if page <= 1 else f"{BASE}/instantnews/index?page={page}"

# ResearchReader 验证过的文章链接正则：
# 形式 /instantnews/<category>/article/<numeric-id>/<url-encoded-title>
LINK_RE = re.compile(r'(?<=href=")(/instantnews/[a-z]+/article/\d+/[^"]*)(?=")')

# 文章 URL 模式（用于发现阶段过滤 + 从 URL 提取数字文章 ID）
ARTICLE_URL_RE = re.compile(
    r"/instantnews/(?P<category>[a-z]+)/article/(?P<article_id>\d+)/"
)


class _TextStripper(_PyHTMLParser):
    """从 HTML 片段中提取纯文本（跳过 script/style）。

    与 ResearchReader 的 _Stripper 等价：只处理 starttag/endtag/data。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = False
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:  # noqa: D102
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:  # noqa: D102
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._out)).strip()


# ---------- 标题 fallback（ResearchReader _extract_title 迁移） ----------


def extract_title(html: str) -> str:
    """提取文章标题，优先级：``<h1>`` → ``og:title`` → ``<title>``。

    完全遵循 ResearchReader 已验证的 fallback 顺序：
    1. ``<h1>`` 内文本（真实页面为干净标题）；
    2. ``property="og:title"`` 的 content；
    3. ``<title>`` 标签，并按常见分隔符去掉站点后缀（" - 信報網站 hkej.com" 等）。
    全部不存在时返回空字符串（由调用方决定是否跳过该文章）。
    """
    # 1. <h1>
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if text:
            return text
    # 2. og:title
    m = re.search(r'property="og:title".*?content="(.*?)"', html, re.DOTALL)
    if m:
        text = m.group(1).strip()
        if text:
            return text
    # 3. <title> tag，去掉站点后缀
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if m:
        text = m.group(1).strip()
        # ResearchReader 验证过的分隔符：' - '、'｜'（U+2032 为笔误但保留兼容）、'|'
        for sep in (" - ", "\u2032", "|"):
            if sep in text:
                text = text.split(sep)[0].strip()
        return text if text else ""
    return ""


# ---------- 正文提取（ResearchReader article-content 逻辑迁移） ----------


def extract_body(html: str) -> str:
    """提取 HKEJ 文章正文（``article-content`` 容器内文本）。

    迁移自 ResearchReader ``scrape()`` 的正文提取部分：
    - 定位 ``article-content`` 起始位置，取其后的 ``</div>`` 之前的片段；
    - 用 HTMLParser 剥离标签并跳过 script/style；
    - 清理残留的 ``article-content'> `` 前缀片段。

    这样导航、广告、页面菜单等容器外的 HTML 不会进入正文。
    """
    marker = html.find("article-content")
    if marker < 0:
        return ""
    rest = html[marker:]
    end = rest.find("</div>")
    if end <= 0:
        return ""
    inner = rest[: end + 6]
    s = _TextStripper()
    s.feed(inner)
    body = s.text()
    # 清理残留 HTML 片段：从 "article-content" 到其起始标签的 ">" 为止
    # （兼容单引号 <div class='article-content'> 与双引号 <div class="article-content">）
    body = re.sub(r"^article-content[^>]*>\s*", "", body)
    return body


def extract_author(html: str) -> list[str]:
    """提取 HKEJ 文章作者（作者出现时通常为 ``.writer`` / ``.author`` 类节点）。

    HKEJ 部分文章带作者署名，部分不带（机构/即时新闻）。这里做宽松提取：
    - 优先 ``meta[name=author]`` / ``meta[property=article:author]``；
    - 其次页面内 class 含 ``writer`` / ``author`` 的节点文本。
    提取不到时返回空列表（laxinwen 对无作者文章完全兼容）。
    """
    authors: list[str] = []
    try:
        tree = HTMLParser(html)
        for meta in tree.css("meta"):
            name = (meta.attributes.get("name") or meta.attributes.get("property") or "").lower()
            if name in ("author", "article:author", "byl"):
                content = (meta.attributes.get("content") or "").strip()
                if content and content not in authors:
                    authors.append(content)
        if authors:
            return authors
        for node in tree.css(".writer, .author, .byline"):
            text = (node.text() or "").strip()
            # 去掉常见前缀词，避免 "作者：xxx" 混入
            for prefix in ("作者", "撰文", "記者", "By ", "by "):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            if text and text not in authors:
                authors.append(text)
    except Exception:
        logger.debug("HKEJ author 提取失败（忽略）", exc_info=True)
    return authors


# ---------- 列表页解析（ResearchReader 抓取循环迁移） ----------


def parse_list_page(html: str) -> list[str]:
    """从 HKEJ 列表页 HTML 提取文章相对链接（保持出现顺序，同页去重）。

    直接复用 ResearchReader 的 ``LINK_RE``；同一次调用内对重复链接去重
    （ResearchReader 用 ``seen: dict[str, None]`` 做同样的事）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in LINK_RE.finditer(html):
        rel = m.group(1)
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _resolve_url(list_url: str, rel: str) -> str:
    """把列表页内相对链接解析为绝对 URL。"""
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    parts = urlsplit(list_url)
    return f"{parts.scheme}://{parts.netloc}{rel}"


def discover_list(
    *,
    fetcher: BaseFetcher,
    max_items: int,
    start_page: int = 1,
) -> list[DiscoveredItem]:
    """抓取 HKEJ 即时新闻列表页并发现文章，直到达到 ``max_items`` 或取不到更多。

    分页逻辑完全遵循 ResearchReader：
    - 第 1 页 ``/instantnews``；
    - 第 2 页起 ``/instantnews/index?page=N``；
    - 逐页抓取，收集所有文章链接；跨页用 canonical URL 去重；
    - 达到 ``max_items`` 立即停止（不再多抓）；
    - 某页抓取失败时记日志并继续下一页（ResearchReader 的容错行为），
      若连续多页失败则停止。

    返回按列表页出现顺序的 DiscoveredItem 列表（不保证全部 ≤ max_items，
    由调用方截断）。标题在列表页已有（ResearchReader 的列表项 anchor 文本），
    一并填充；发布时间/正文留在 pipeline 下载后由通用提取补全。
    """
    collected: list[DiscoveredItem] = []
    seen: set[str] = set()
    page = start_page
    consecutive_failures = 0
    consecutive_empty = 0

    while len(collected) < max_items:
        url = _list_url(page)
        logger.info("[hkej] 列表页 %s", url)
        try:
            html = fetcher.fetch(url)
        except Exception as exc:
            logger.warning("[hkej] 列表页抓取失败 %s: %s", url, exc)
            consecutive_failures += 1
            if consecutive_failures >= 2:
                logger.warning("[hkej] 连续 %d 页失败，停止翻页", consecutive_failures)
                break
            page += 1
            continue

        consecutive_failures = 0
        rels = parse_list_page(html)
        if not rels:
            logger.info("[hkej] 列表页 %s 无文章链接，停止翻页", url)
            break

        added = 0
        for rel in rels:
            if len(collected) >= max_items:
                break
            canon = canonicalize_url(_resolve_url(LIST_URL, rel))
            if not canon or canon in seen:
                continue
            seen.add(canon)
            collected.append(
                DiscoveredItem(url=canon, title=_title_from_url(rel))
            )
            added += 1
        logger.info("[hkej] 列表页 %s 新增 %d 条（累计 %d）", url, added, len(collected))

        # 一页没有任何新链接（分页循环回到已收集文章）→ 停止，避免无限翻页
        if added == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                logger.info("[hkej] 连续 %d 页无新链接，停止翻页", consecutive_empty)
                break
        else:
            consecutive_empty = 0
        page += 1

    return collected


def _title_from_url(rel: str) -> str:
    """从文章 URL 的编码标题片段解码出标题（ResearchReader 也用 URL 标题做预览）。

    HKEJ 列表项 anchor 文本即为标题，但 ``parse_list_page`` 只返回 href。
    这里从 URL 末段解码标题作为发现阶段标题；真正的完整标题在下载正文后
    由 ``extract_title``（h1 → og:title → title）覆盖。
    """
    path = rel.split("?")[0]
    seg = path.rstrip("/").rsplit("/", 1)[-1]
    try:
        text = unquote(seg)
    except Exception:
        return ""
    return text.replace("+", " ").strip()


# ---------- 文章数字 ID（增强去重依据） ----------


def extract_article_id(url: str) -> Optional[str]:
    """从 HKEJ 文章 URL 提取稳定的数字文章 ID（若存在）。

    形如 ``/instantnews/<category>/article/<id>/<title>`` 的 URL，
    返回 ``<id>``；否则返回 None。数字 ID 是 HKEJ 文章的稳定标识，
    可作为增强去重依据，但**不会替换** laxinwen 现有 canonical URL 去重。
    """
    m = ARTICLE_URL_RE.search(url)
    if m:
        return m.group("article_id")
    return None


# ---------- Adapter ----------


class HkejAdapter(SourceAdapter):
    """HKEJ 信報財經新聞 Source Adapter。

    只负责发现新闻 URL + 标题；正文/时间/作者由 pipeline 下载后完成。
    """

    def __init__(self, source_id: str, source_name: str) -> None:
        super().__init__(source_id, source_name)
        self.base = BASE

    def discover(self, *, fetcher: BaseFetcher, max_items: int) -> list[DiscoveredItem]:
        items = discover_list(fetcher=fetcher, max_items=max_items)
        # 达到 max_items 后按需截断（discover_list 内部已按 max_items 停止）
        return items[:max_items]

    def fetch_custom_headers(self) -> Optional[dict[str, str]]:
        # HKEJ 对非浏览器 UA 拒绝访问（ResearchReader 验证过），
        # 用桌面 Chrome UA + 完整 Accept-Language（zh-HK）。
        return {
            "User-Agent": HKEJ_UA,
            "Accept-Language": "zh-HK,zh;q=0.9,zh-CN;q=0.8,en;q=0.7",
            "Referer": f"{BASE}/instantnews",
        }
