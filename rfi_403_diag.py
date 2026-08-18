#!/usr/bin/env python3
"""RFI 官网 403 诊断脚本（Windows 实机运行）。

用法（在仓库根目录，项目依赖已安装的环境）：
    python rfi_403_diag.py

它会依次执行 5 组实验并打印 status / content-length / 请求头 / 响应头：

  ① HttpxFetcher → 文章页       （走项目自带 FetcherOptions 默认逻辑）
  ② 裸 httpx     → 文章页       （不经过项目封装，纯 httpx.get）
  ③ HttpxFetcher → 首页
  ④ HttpxFetcher(浏览器UA) → 文章页
  ⑤ 冷却 10 秒后再请求 文章页     （验证是否被 Akamai 短时限流）

「文章页」URL 会先通过抓取 RFI 首页自动发现一个真实文章链接；
若发现失败可手动改下方的 ``ARTICLE_URL``。

不修改任何项目代码，仅作为诊断工具使用。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 确保能从仓库根目录 import 到项目 src 包
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import httpx  # noqa: E402

from news.fetch import FetcherOptions, HttpxFetcher  # noqa: E402

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------
HOMEPAGE_URL = "https://www.rfi.fr/cn/"

# 若自动发现文章链接失败，可手动指定一个真实文章 URL
ARTICLE_URL = ""

# 浏览器风格 UA（实验④用）
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _summarize_headers(headers: httpx.Headers) -> dict:
    """挑出诊断关键响应头。"""
    out = {}
    for key in (
        "content-type",
        "content-length",
        "server",
        "via",
        "x-cache",
        "x-akamai-*",
        "set-cookie",
        "location",
        "retry-after",
        "date",
    ):
        if key == "x-akamai-*":
            for k, v in headers.items():
                if k.lower().startswith("x-akamai"):
                    out[k] = v
        elif key in headers:
            out[key] = headers[key]
    return out


def _print_request_headers(req_headers: dict) -> None:
    print("  ┌─ 请求头（发送）")
    for k, v in req_headers.items():
        # 打码 UA，避免输出过长
        if k.lower() == "user-agent":
            v = v[:90] + ("…" if len(v) > 90 else "")
        print(f"  │   {k}: {v}")
    print("  └")


def _print_response_summary(resp: httpx.Response) -> None:
    print(f"  STATUS      : {resp.status_code}")
    print(f"  Content-Len : {resp.headers.get('content-length', 'N/A')}")
    print(f"  Body chars  : {len(resp.text)}")
    print("  ┌─ 响应头（关键）")
    for k, v in _summarize_headers(resp.headers).items():
        if k.lower() == "set-cookie":
            v = v[:80] + ("…" if len(v) > 80 else "")
        print(f"  │   {k}: {v}")
    print("  └")


def _safe_fetch(label: str, fn, **kwargs) -> None:
    """统一执行一次请求并打印结果，捕获异常。"""
    print(f"\n{'=' * 70}")
    print(f"▶ {label}")
    print(f"  URL: {kwargs.get('url', '')}")
    try:
        result = fn()
        if isinstance(result, httpx.Response):
            resp = result
            _print_request_headers(dict(resp.request.headers))
            _print_response_summary(resp)
        elif isinstance(result, str):
            print(f"  (fetch 返回 str，长度 {len(result)} 字符)")
        else:
            print(f"  (返回类型: {type(result).__name__})")
    except Exception as exc:
        status = getattr(exc, "status", None)
        print(f"  ❌ 异常: {type(exc).__name__}: {exc}")
        if status:
            print(f"  STATUS      : {status}")
    print(f"{'=' * 70}")


# ---------------------------------------------------------------------------
# 自动发现一个真实文章 URL
# ---------------------------------------------------------------------------
def _discover_article_url() -> str:
    """通过抓取 RFI 首页，解析出一个文章链接。失败返回 ''。"""
    try:
        r = httpx.get(HOMEPAGE_URL, follow_redirects=True, timeout=20)
        if r.status_code != 200:
            print(f"[discover] 首页返回 {r.status_code}，无法自动发现文章 URL")
            return ""
        import re

        # RFI 文章 URL 模式：/cn/<分类>/<YYYYMMDD>-<slug>
        m = re.search(r'https://www\.rfi\.fr/cn/[^"\'\s<>]+/\d{8}-[^"\'\s<>"&]+', r.text)
        if not m:
            print("[discover] 首页 HTML 中未匹配到文章链接")
            return ""
        url = m.group(0).rstrip('"\'.,;)')
        print(f"[discover] 自动发现文章 URL: {url}")
        return url
    except Exception as exc:
        print(f"[discover] 自动发现失败: {exc}")
        return ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("RFI 官网 403 诊断脚本")
    print(f"Python: {sys.version}")
    print(f"httpx : {httpx.__version__}")
    print()

    article_url = ARTICLE_URL or _discover_article_url()
    if not article_url:
        print("⚠ 未找到文章 URL，请在脚本中手动设置 ARTICLE_URL 后重跑。")
        return

    print(f"首页 URL  : {HOMEPAGE_URL}")
    print(f"文章 URL  : {article_url}")
    print()

    # ---------- 实验①  HttpxFetcher → 文章页 ----------
    def test_1():
        fetcher = HttpxFetcher()  # 默认 FetcherOptions
        try:
            return fetcher.fetch(article_url)
        finally:
            fetcher.close()

    _safe_fetch("① HttpxFetcher(默认配置) → 文章页", test_1, url=article_url)

    # ---------- 实验②  裸 httpx → 文章页 ----------
    def test_2():
        return httpx.get(article_url, follow_redirects=True, timeout=20)

    _safe_fetch("② 裸 httpx（不经过项目封装）→ 文章页", test_2, url=article_url)

    # ---------- 实验③  HttpxFetcher → 首页 ----------
    def test_3():
        fetcher = HttpxFetcher()
        try:
            return fetcher.fetch(HOMEPAGE_URL)
        finally:
            fetcher.close()

    _safe_fetch("③ HttpxFetcher(默认配置) → 首页", test_3, url=HOMEPAGE_URL)

    # ---------- 实验④  浏览器 UA → 文章页 ----------
    def test_4():
        options = FetcherOptions(user_agent=BROWSER_UA, retries=0)
        fetcher = HttpxFetcher(options)
        try:
            return fetcher.fetch(article_url)
        finally:
            fetcher.close()

    _safe_fetch("④ HttpxFetcher(浏览器 Chrome UA) → 文章页", test_4, url=article_url)

    # ---------- 实验⑤  冷却 10 秒后再试文章页 ----------
    print("\n冷却 10 秒中…")
    time.sleep(10)

    def test_5():
        fetcher = HttpxFetcher()
        try:
            return fetcher.fetch(article_url)
        finally:
            fetcher.close()

    _safe_fetch("⑤ 冷却10秒后 HttpxFetcher(默认配置) → 文章页", test_5, url=article_url)

    print("\n✅ 全部实验执行完毕。")


if __name__ == "__main__":
    main()
