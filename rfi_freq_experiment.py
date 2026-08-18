#!/usr/bin/env python3
"""RFI 文章请求频率实验（Windows 实机运行）。

用法（在仓库根目录，项目依赖已安装的环境）：
    uv run python rfi_freq_experiment.py [<文章 URL>]

不带参数时，默认使用内置的测试文章 URL；也可通过命令行参数指定：
    uv run python rfi_freq_experiment.py "https://www.rfi.fr/cn/..."

实验内容（严格按顺序执行，不做项目级节流，纯测 15 秒手工间隔）：

  实验 1  默认 HttpxFetcher 单独请求目标文章 → status + 正文长度
  实验 2  同一文章连续请求 3 次，每次间隔 15 秒 → 时间 / status / 正文长度
  实验 3  若实验 2 三次全部 200，再连续请求 3 篇不同的 RFI 文章，
          每篇间隔 15 秒 → 时间 / URL / status / 正文长度

说明：
  - 每个请求都新建 HttpxFetcher，不使用项目级节流（min_interval 仅对同一
    fetcher 实例生效，跨实例无效），纯粹测试 15 秒手工间隔是否足以避免 403。
  - 使用默认 FetcherOptions（min_interval=2.0、retries=3）。
  - 3 篇不同文章会在实验 3 开始前从 RFI 亚洲分类页预先发现，避免穿插请求
    干扰时间序列。

不修改任何项目代码，仅作为诊断工具使用。
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
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
# 目标测试文章（默认使用用户指定的、实际已出现 403 的文章）
DEFAULT_ARTICLE_URL = (
    "https://www.rfi.fr/cn/亚洲/20260818-解放报-在东南亚，"
    "俄罗斯梦想成为大国，但仍旧是个二流角色"
)

# RFI 亚洲分类页（用于实验 3 预先发现 3 篇不同文章）
ASIA_CATEGORY_URL = "https://www.rfi.fr/cn/亚洲"

# 实验间隔（秒）
INTERVAL_SECONDS = 15.0

# RFI 文章 URL 模式：/cn/<分类>/<YYYYMMDD>-<slug>
_RFI_ARTICLE_URL_RE = re.compile(r'https://www\.rfi\.fr/cn/[^"\'\s<>]+/\d{8}-[^"\'\s<>"&]+')


def _now_str() -> str:
    """返回形如 HH:MM:SS 的当前时间字符串。"""
    return datetime.now().strftime("%H:%M:%S")


def _request_with_status(url: str) -> tuple[int | None, int, str]:
    """用默认 HttpxFetcher 请求 URL，返回 (status, content_length, 说明)。

    因为 ``HttpxFetcher.fetch()`` 对 4xx 直接抛 FetchError，拿不到 status，
    这里直接用 ``_client.get()`` 发送请求（与 fetch() 内部使用同一个 httpx
    Client，同样的 headers / UA / timeout / follow_redirects），从而能拿到
    真实的 HTTP status 码。
    """
    fetcher = HttpxFetcher()  # 默认 FetcherOptions
    try:
        resp = fetcher._client.get(url)
        status = resp.status_code
        length = len(resp.content)
        return status, length, ""
    except httpx.TimeoutException as exc:
        return None, 0, f"Timeout: {type(exc).__name__}"
    except httpx.HTTPError as exc:
        return None, 0, f"HTTPError: {type(exc).__name__}: {exc}"
    except Exception as exc:
        return None, 0, f"Error: {type(exc).__name__}: {exc}"
    finally:
        fetcher.close()


def _discover_articles_from_asia(max_count: int = 3) -> list[str]:
    """从 RFI 亚洲分类页发现 max_count 篇真实文章 URL（排除目标文章自身）。"""
    try:
        fetcher = HttpxFetcher()
        try:
            html = fetcher._client.get(ASIA_CATEGORY_URL)
            if html.status_code != 200:
                print(f"[discover] 亚洲分类页返回 {html.status_code}，无法发现文章")
                return []
            urls = list(dict.fromkeys(_RFI_ARTICLE_URL_RE.findall(html.text)))
        finally:
            fetcher.close()
    except Exception as exc:
        print(f"[discover] 亚洲分类页请求失败: {exc}")
        return []

    # 去掉目标文章自身
    urls = [u for u in urls if u != DEFAULT_ARTICLE_URL]
    print(f"[discover] 从亚洲分类页发现 {len(urls)} 篇文章 URL")
    return urls[:max_count]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("RFI 文章请求频率实验")
    print(f"Python: {sys.version}")
    print(f"httpx : {httpx.__version__}")
    print()

    # 目标文章 URL：优先命令行参数，其次内置默认
    if len(sys.argv) > 1 and sys.argv[1].startswith(("http://", "https://")):
        article_url = sys.argv[1].strip().rstrip('"\'')
        print(f"[argv] 使用命令行参数作为目标文章 URL")
    else:
        article_url = DEFAULT_ARTICLE_URL
        print(f"[default] 使用内置默认文章 URL")

    print(f"目标文章 : {article_url}")
    print()

    # ---------- 实验 1：单独请求 ----------
    print("=" * 70)
    print(f"▶ 实验 1：默认 HttpxFetcher 单独请求目标文章")
    print(f"  时间: {_now_str()}")
    status, length, note = _request_with_status(article_url)
    if note:
        print(f"  STATUS : N/A ({note})")
        print(f"  正文长度: 0")
    else:
        print(f"  STATUS : {status}")
        print(f"  正文长度: {length} bytes")
    print("=" * 70)

    # ---------- 实验 2：同一文章连发 3 次，间隔 15 秒 ----------
    print()
    print("=" * 70)
    print(f"▶ 实验 2：同一文章连续请求 3 次，每次间隔 {INTERVAL_SECONDS:.0f} 秒")
    results_2: list[tuple[str, int | None, int, str]] = []
    for i in range(1, 4):
        print(f"\n  --- 第 {i} 次请求 @ {_now_str()} ---")
        status, length, note = _request_with_status(article_url)
        if note:
            print(f"  STATUS : N/A ({note})")
            print(f"  正文长度: 0")
            results_2.append((_now_str(), None, 0, note))
        else:
            print(f"  STATUS : {status}")
            print(f"  正文长度: {length} bytes")
            results_2.append((_now_str(), status, length, ""))

        if i < 3:
            print(f"  → 等待 {INTERVAL_SECONDS:.0f} 秒…")
            time.sleep(INTERVAL_SECONDS)

    all_200 = all(status == 200 for _, status, _, _ in results_2)
    print(f"\n  实验 2 结果: {'全部 200 ✅' if all_200 else '存在非 200 ❌'}")
    print("=" * 70)

    # ---------- 实验 3：若实验 2 全部 200，连发 3 篇不同文章 ----------
    if not all_200:
        print("\n⚠ 实验 2 未全部返回 200，跳过实验 3。")
        print("判断：15 秒 article 间隔不足以避免 RFI 的 403。")
        return

    print()
    print("=" * 70)
    print(f"▶ 实验 3：连续请求 3 篇不同 RFI 文章，每篇间隔 {INTERVAL_SECONDS:.0f} 秒")
    print("  先从亚洲分类页预先发现 3 篇不同文章…")
    other_articles = _discover_articles_from_asia(max_count=3)

    if not other_articles:
        print("  ⚠ 无法从亚洲分类页发现文章，实验 3 跳过。")
        print("=" * 70)
        return

    print(f"  发现文章 {len(other_articles)} 篇：")
    for j, u in enumerate(other_articles, 1):
        print(f"    {j}. {u}")
    print()

    for j, url in enumerate(other_articles, 1):
        print(f"  --- 第 {j} 篇 @ {_now_str()} ---")
        print(f"  URL: {url}")
        status, length, note = _request_with_status(url)
        if note:
            print(f"  STATUS : N/A ({note})")
            print(f"  正文长度: 0")
        else:
            print(f"  STATUS : {status}")
            print(f"  正文长度: {length} bytes")

        if j < len(other_articles):
            print(f"  → 等待 {INTERVAL_SECONDS:.0f} 秒…")
            time.sleep(INTERVAL_SECONDS)

    print()
    print("=" * 70)
    print("✅ 全部实验执行完毕。")
    print("判断：")
    print("  - 若实验 2/3 全部返回 200，说明 15 秒 article 间隔足以避免 RFI 403。")
    print("  - 若出现任何 403，说明 15 秒间隔不够，需要增大间隔或降低请求频率。")
    print("=" * 70)


if __name__ == "__main__":
    main()
