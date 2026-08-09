"""URL 规范化与标题指纹 —— 两层去重的基础。

第一层：canonical URL 去重。
第二层：标题 fingerprint 去重（同源内近似标题）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 需要移除的追踪参数
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "cmpid",
    "s_kwcid",
    "gbraid",
    "wbraid",
}

# 常见站点名称后缀，用于标题指纹清洗
_COMMON_SITE_SUFFIXES = [
    " - ECO",
    " | ECO",
    " – ECO",
    " — ECO",
    " - 信報",
    " - 信報財經新聞",
    " - 香港經濟日報",
    " | 信報",
    " - BBC News",
    " | Reuters",
    " - Reuters",
    " - Financial Times",
    " | Financial Times",
]

# 无意义标点（指纹清洗时移除，但保留空白与字母数字）
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def canonicalize_url(url: str) -> str:
    """规范化 URL 用于去重。

    处理项：
    - 去 fragment
    - 去 utm_* / fbclid / gclid 等追踪参数
    - 域名小写
    - 去掉默认端口
    - 保留其余 path / query 原样（不重排参数，避免误判）
    """
    if not url:
        return ""
    url = url.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    # 去掉默认端口
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    # 过滤追踪参数
    keep: list[tuple[str, str]] = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower() not in _TRACKING_PARAMS:
            keep.append((k, v))
    query = urlencode(keep)
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def title_fingerprint(title: str, *, site_suffixes: list[str] | None = None) -> str:
    """计算标题指纹。

    步骤：
    1. Unicode NFKC 归一化
    2. 去除常见站点名称后缀
    3. 大小写归一化（小写）
    4. 去除多余空白
    5. 去除无意义标点，仅保留字母数字
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    # 去除组合变音符号（í → i，方便标题比对）
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    for suffix in _COMMON_SITE_SUFFIXES + (site_suffixes or []):
        if text.strip().endswith(suffix.strip()):
            text = text[: -len(suffix)].strip()
            break
    text = text.lower()
    text = _WHITESPACE_RE.sub(" ", text)   # 折叠多余空白为单个空格
    # 去除无意义标点（保留空白与字母数字），再折叠一次标点删除后留下的空白
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def fingerprint_sha256(title: str, *, site_suffixes: list[str] | None = None) -> str:
    """返回标题指纹的 SHA-256 摘要（用于数据库索引）。"""
    fp = title_fingerprint(title, site_suffixes=site_suffixes)
    if not fp:
        return ""
    return hashlib.sha256(fp.encode("utf-8")).hexdigest()
