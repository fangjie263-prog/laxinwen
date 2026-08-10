"""便携式 HTML 导出 —— 「独立 HTML」与「HTML 新闻包」。

目标（对应需求「导出层 + 时间显示层 + GUI 按钮」，不触碰抓取/数据库/AI/Reader 核心架构）：

- **独立 HTML**：把「Daily Reader」整体打包成**单个 self-contained** ``.html`` 文件。
  CSS/JS 全部内嵌，不依赖 localhost / laxinwen / 外部 CDN / 本地 Python 服务 / 项目其它 CSS/JS。
  双击即可直接阅读（在没有安装 laxinwen 的电脑上也能用）。
- **HTML 新闻包**：生成 ``data/export/portable/<站点>-<日期>/`` 目录，
  含自包含的 ``index.html``（同独立 HTML）+ ``articles/NNN.html`` 单篇页，
  可整体复制到其它电脑，双击 ``index.html`` 阅读。

关键点：
- 复用 ``news_archive`` 的 Daily Reader 设计语言（``_READER_CSS`` / ``_READER_JS``）与数据读取，
  不重新发明 Reader；
- AI 研究信息（关键观点/主题/实体/市场相关性/语言）**内嵌**到文章区块（``<details>``），
  保证独立文件不依赖其它 HTML 也能读到完整 AI 详情；
- 所有展示时间统一北京时间（Asia/Shanghai），24 小时制（复用 ``beijing``）；
- 用户/文章内容全部 HTML escape。
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .beijing import fmt_date as _bj_fmt_date, fmt_dt as _bj_fmt_dt
from .news_archive import (
    _READER_CSS,
    _READER_JS,
    _ai_status,
    _ai_status_html,
    _build_filename,
    _e,
    _load_json,
    _parse_datetime,
    render_article_page,
    render_article_section,
    slugify,
)
from .storage import Storage

logger = logging.getLogger(__name__)


# ======================================================================
# 「便携阅读包」内嵌的迷你本地 HTTP 阅读服务器（纯 Python 标准库）。
#
# 被 Open-Reader.bat 调用：先启动一个只监听 127.0.0.1 的静态服务器，
# 自动选择空闲端口，然后打开默认浏览器访问
# ``http://127.0.0.1:<port>/index.html``（而非 file://），
# 从而让浏览器扩展（如沉浸式翻译）能正常识别网页正文。
#
# 不依赖 laxinwen / SQLite / 第三方库 / 互联网 / API Key。
# 关闭控制台窗口（或按 Ctrl+C）即可结束服务器。
# ======================================================================
_PORTABLE_SERVER_PY = r'''# -*- coding: utf-8 -*-
# Laxinwen 便携阅读包 - 本地 HTTP 阅读服务器
#
# 纯 Python 标准库，无需安装 laxinwen / SQLite / 任何第三方依赖。
# 只监听 127.0.0.1（本机），不暴露到局域网，不需要访问互联网。
import sys
import webbrowser
import socketserver
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"


class _QuietHandler(SimpleHTTPRequestHandler):
    """静默版静态文件 handler（不把每次请求打印到控制台）。"""
    def log_message(self, fmt, *args):  # noqa: A003
        pass


class _Server(ThreadingHTTPServer):
    """只服务 127.0.0.1 的服务器（避免 getfqdn 潜在崩溃）。"""
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def _start(directory, port):
    handler = partial(_QuietHandler, directory=directory)
    # 自动选择空闲端口：显式端口 > 8000-8009 > 系统随机空闲端口
    candidates = [port] if port else list(range(8000, 8010)) + [0]
    for p in candidates:
        try:
            httpd = _Server((HOST, p), handler)
            return httpd
        except OSError:
            continue
    raise OSError("无法绑定 127.0.0.1 的任何候选端口")


def main():
    directory = sys.argv[1] if len(sys.argv) > 1 else "."
    port = 0
    if len(sys.argv) > 2:
        try:
            port = int(sys.argv[2])
        except ValueError:
            port = 0

    httpd = _start(directory, port)
    actual_port = httpd.server_address[1]
    url = "http://{0}:{1}/index.html".format(HOST, actual_port)

    print("=" * 56)
    print("Laxinwen 便携阅读包 - 本地阅读服务器已启动")
    print("  地址: " + url)
    print("  仅本机可访问 (127.0.0.1)，不暴露到局域网。")
    print("  正在打开默认浏览器……")
    print("  关闭本窗口（或按 Ctrl+C）即可结束阅读服务器。")
    print("=" * 56)

    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("发生错误:", exc)
        input("按回车键退出...")
        sys.exit(1)
'''


# ======================================================================
# 「便携阅读包」的启动器 Open-Reader.bat（Windows 双击运行）。
#
# 约束：
#   - 不含任何 API Key / 敏感信息；
#   - 不含绝对路径，只依赖脚本所在目录 ``%~dp0``；
#   - 不绑定某台电脑的具体路径（可复制到任意 Windows 电脑）；
#   - 优先寻找本机 Python 3（py / python / python3）；
#     找不到时给出清晰提示，而不是静默失败。
# ======================================================================
_OPEN_READER_BAT = r'''@echo off
rem ============================================================
rem  Laxinwen 便携阅读包 - 本地阅读启动器
rem  双击本文件即可在本机浏览器中阅读新闻包。
rem
rem  原理：
rem    1. 用 Python 标准库 http.server 起一个只监听 127.0.0.1
rem       的本地静态服务器，自动选择空闲端口。
rem    2. 自动打开默认浏览器: http://127.0.0.1:<port>/index.html
rem       而非 file://，从而浏览器扩展（如沉浸式翻译）可正常工作。
rem    3. 无需安装 laxinwen / SQLite / 任何第三方库 / 互联网 / API Key。
rem    4. 关闭本窗口即可结束阅读服务器。
rem
rem  安全说明：
rem    - 本文件不包含任何 API Key / 敏感信息
rem    - 不含绝对路径，只依赖脚本所在目录 (%~dp0)
rem    - 只监听 127.0.0.1，不暴露到局域网
rem ============================================================
setlocal
cd /d "%~dp0"

rem ---- 优先寻找本机可用的 Python 3（py / python / python3）----
set "PYTHON="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
  where python3 >nul 2>nul
  if not errorlevel 1 set "PYTHON=python3"
)

if not defined PYTHON (
  echo.
  echo [错误] 未在本机找到 Python 3。
  echo   便携阅读包需要 Python 3 才能启动本地阅读服务器。
  echo   请到 https://www.python.org/downloads/ 安装 Python 3，
  echo   安装时勾选 "Add Python to PATH"。
  echo.
  echo   安装完成后重新双击本文件即可。
  echo.
  pause
  exit /b 1
)

if not exist "server.py" (
  echo.
  echo [错误] 找不到 server.py，请确认便携阅读包文件完整。
  pause
  exit /b 1
)

echo 正在启动本地阅读服务器（仅 127.0.0.1，自动选择空闲端口）...
%PYTHON% server.py

endlocal
'''


@dataclass
class PortableResult:
    """便携式导出统计。"""

    exported: int = 0
    failed: int = 0
    analyzed_ok: int = 0
    analyzed_failed: int = 0
    unanalyzed: int = 0
    files: list[Path] = field(default_factory=list)
    index_path: Optional[Path] = None

    def as_dict(self) -> dict:
        return {
            "exported": self.exported,
            "failed": self.failed,
            "analyzed_ok": self.analyzed_ok,
            "analyzed_failed": self.analyzed_failed,
            "unanalyzed": self.unanalyzed,
        }


def _normalize_rows(rows) -> list[dict]:
    """把 SQLite 行/字典统一转成 dict 列表，补齐解析所需的字段。"""
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        out.append(row)
    return out


def _embed_ai_details(analysis: dict[str, Any]) -> str:
    """把 AI 研究详情（关键观点/主题/实体/市场相关性/语言）内嵌为可展开区块。

    独立 HTML 不能依赖外部 AI Research 页面，因此把详情直接放进 ``<details>``。
    """
    if not analysis:
        return ""
    key_points = analysis.get("key_points") or []
    topics = analysis.get("topics") or []
    entities = analysis.get("entities") or []
    relevance = (analysis.get("market_relevance") or "").strip().lower()
    reason = analysis.get("market_relevance_reason") or ""
    lang = analysis.get("language") or ""

    mr_label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(
        relevance, (relevance or "—").upper()
    )
    mr_cls = {"high": "mr-high", "medium": "mr-medium", "low": "mr-low"}.get(
        relevance, "mr-medium"
    )

    parts: list[str] = []
    kp = "".join(f"<li>{_e(k)}</li>" for k in key_points)
    parts.append(f"<h3 style='font-size:14px;margin:10px 0 4px;'>关键观点</h3>")
    parts.append(f'<ol class="key-points">{kp}</ol>' if kp else '<p class="ai-note">（无）</p>')
    chips = "".join(f'<span class="chip">{_e(t)}</span>' for t in topics)
    parts.append("<h3 style='font-size:14px;margin:10px 0 4px;'>主题</h3>")
    parts.append(f'<div class="chips">{chips}</div>' if chips else '<p class="ai-note">（无）</p>')

    ent_rows = ""
    for ent in entities:
        if isinstance(ent, dict):
            ent_rows += f"<tr><td>{_e(ent.get('name'))}</td><td>{_e(ent.get('type'))}</td></tr>"
    parts.append("<h3 style='font-size:14px;margin:10px 0 4px;'>实体</h3>")
    parts.append(
        '<table class="entities"><thead><tr><th>实体</th><th>类型</th></tr></thead>'
        f"<tbody>{ent_rows}</tbody></table>"
        if ent_rows
        else '<p class="ai-note">（无）</p>'
    )

    parts.append("<h3 style='font-size:14px;margin:10px 0 4px;'>市场相关性</h3>")
    parts.append(
        f'<p><span class="mr-badge {mr_cls}">{_e(mr_label)}</span> '
        f'<span class="ai-note">（AI 判断，不代表原文事实）</span></p>'
    )
    if reason:
        parts.append(f'<p class="ai-note"><strong>分析理由：</strong> {_e(reason)}</p>')
    parts.append(
        f'<h3 style="font-size:14px;margin:10px 0 4px;">原文语言</h3>'
        f'<p class="ai-note">{_e(lang) if lang else "—"}</p>'
    )

    return (
        '<details class="ai-details"><summary>展开 AI 研究详情（内嵌）</summary>'
        + "".join(parts)
        + "</details>"
    )


def _toc_entries(rows: list) -> list[dict]:
    out: list[dict] = []
    for i, row in enumerate(rows, start=1):
        r = dict(row)
        out.append({"n": i, "article_id": int(r["id"]), "title": r["title"] or ""})
    return out


def _render_toc_html(entries: list[dict]) -> str:
    items = "".join(
        f'<li><a href="#article-{e["n"]}">{_e(e["title"])}</a></li>' for e in entries
    )
    return (
        '<nav class="toc" id="toc" aria-label="Table of Contents">'
        f'<h2>Table of Contents</h2><ol>{items}</ol></nav>'
    )


def render_independent_html(
    rows,
    *,
    source_name: str,
    total: int,
    analyzed_ok: int,
    analyzed_failed: int,
    unanalyzed: int,
    research_rel_by_article: dict[int, str],
    generated_at: datetime,
    analysis_by_article: dict[int, dict[str, Any]],
    article_pages: Optional[dict[int, str]] = None,
) -> str:
    """渲染「独立 HTML」：单个自包含文件，内嵌全部新闻 + CSS/JS + AI 详情。

    ``article_pages``（可选）：article_id → articles/NNN.html 相对路径（HTML 新闻包用），
    非空时每篇额外提供「查看单篇」链接。
    """
    rows = _normalize_rows(rows)
    article_pages = article_pages or {}

    sections: list[str] = []
    for i, row in enumerate(rows, start=1):
        article_id = int(row["id"])
        try:
            authors = json.loads(row.get("authors") or "[]")
        except (ValueError, TypeError):
            authors = []
        status = _ai_status(row)
        analysis = analysis_by_article.get(article_id, {})
        research_rel = research_rel_by_article.get(article_id, "")

        # 单篇链接（HTML 新闻包内跳转）
        per_page = article_pages.get(article_id)
        extra_link = ""
        if per_page:
            extra_link = (
                f'<a class="btn btn-outline" href="{_e(per_page)}" '
                f'rel="noopener">查看单篇页 →</a>'
            )

        section = render_article_section(
            index=i,
            article_id=article_id,
            title=row.get("title") or "",
            source_name=row.get("source_name") or row.get("source_id") or source_name,
            authors=authors,
            published_at=(
                _parse_datetime(row.get("published_at"))
                or _parse_datetime(row.get("discovered_at"))
            ),
            canonical_url=row.get("canonical_url") or "",
            body_text=row.get("body_text") or "",
            ai_status=status,
            analysis=analysis,
            research_rel=research_rel,
        )
        # 内嵌 AI 详情
        if status == "ok":
            embed = _embed_ai_details(analysis)
            if embed:
                marker = '<div class="original-body">'
                if marker in section:
                    section = section.replace(marker, embed + "\n" + marker, 1)
                else:
                    section = section + embed
        # 注入单篇链接到 links 区
        if extra_link:
            section = section.replace(
                'class="article-links">',
                'class="article-links">' + extra_link,
                1,
            )
        sections.append(section)
    sections_html = "\n".join(sections) if sections else (
        '<p class="empty-body">（数据库暂无新闻）</p>'
    )

    stats_line = (
        f'AI 状态：{_ai_status_html("ok")} {analyzed_ok} · '
        f'{_ai_status_html("failed")} {analyzed_failed} · '
        f'{_ai_status_html("none")} {unanalyzed}'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(source_name)} News — 便携阅读器 · laxinwen</title>
<style>{_READER_CSS}</style>
</head>
<body>

<div class="toolbar"><div id="progress-bar"></div></div>
<div class="reader-controls">
  <span class="mode-label" id="mode-label">Day</span>
  <button type="button" id="mode-toggle" title="切换阅读模式（Day / Sepia / Night）">阅读模式</button>
</div>

<div class="reader">

  <header class="masthead">
    <h1>{_e(source_name)} News — 便携阅读器</h1>
    <p class="date">{_bj_fmt_date(generated_at)}</p>
    <p class="count">{total} articles · {_e(source_name)}</p>
    <p class="sub">
      最近 {total} 条 · 按发布日期倒序 · 更新时间 {_bj_fmt_dt(generated_at)}（北京时间）
      <br>{stats_line}
      <br>独立自包含 HTML，无需安装 laxinwen / Python / 本地服务器，双击即可阅读。
    </p>
  </header>

  {_render_toc_html(_toc_entries(rows))}

  <main>
    {sections_html}
  </main>

  <div class="footer">
    由 laxinwen 生成 · 便携式 Daily Reader（最近 {total} 条新闻） ·
    快捷键 J/K 切换上下篇 · □/☆ 已读/收藏（localStorage 保存）
  </div>
</div>

<script>{_READER_JS}</script>
</body>
</html>
"""


def _collect_context(
    storage: Storage, rows, out_dir: Path, research_root: Optional[Path]
) -> dict:
    """收集导出所需的：analysis_by_article / research_rel_by_article。"""
    analysis_by_article: dict[int, dict[str, Any]] = {}
    research_rel_by_article: dict[int, str] = {}

    for row in rows:
        article_id = int(row["id"])
        if _ai_status(row) == "ok":
            arow = storage.get_analysis_for_article(article_id)
            if arow is not None:
                analysis_by_article[article_id] = {
                    "summary_zh": arow["summary_zh"] or "",
                    "key_points": _load_json(arow["key_points_json"], []),
                    "topics": _load_json(arow["topics_json"], []),
                    "entities": _load_json(arow["entities_json"], []),
                    "market_relevance": arow["market_relevance"] or "",
                    "market_relevance_reason": arow["market_relevance_reason"] or "",
                    "language": arow["language"] or "",
                    "provider": arow["provider"] or "",
                    "model": arow["model"] or "",
                }

    # AI Research 页面路径（若存在则保留 Research 链接；独立 HTML 用绝对/相对路径均可）
    if research_root is not None and research_root.is_dir():
        for rrow in storage.list_analysis_success(source_id=None, limit=10**9):
            art_id = int(rrow["article_id"])
            pub = _parse_datetime(rrow["published_at"]) or _parse_datetime(rrow["discovered_at"])
            fname = _build_filename(art_id, rrow["art_title"] or "", pub)
            month = f"{pub:%Y}/{pub:%m}" if pub else "unknown/00"
            research_rel_by_article[art_id] = (
                f"{research_root.as_posix()}/{month}/{fname}"
            )

    return {
        "analysis_by_article": analysis_by_article,
        "research_rel_by_article": research_rel_by_article,
    }


def export_independent_html(
    storage: Storage,
    out_path: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int = 100,
    research_root: Optional[str | Path] = None,
) -> PortableResult:
    """导出「独立 HTML」：单个 self-contained ``.html`` 文件。

    输出：``out_path``（默认 ``data/export/portable/<site>-<date>.html``）。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = storage.list_articles_with_analysis(
        source_id=source_id, limit=limit if limit else 10**9
    )
    result = PortableResult()
    result.exported = len(rows)
    for row in rows:
        status = _ai_status(row)
        if status == "ok":
            result.analyzed_ok += 1
        elif status == "failed":
            result.analyzed_failed += 1
        else:
            result.unanalyzed += 1

    research_root = Path(research_root) if research_root else None
    ctx = _collect_context(storage, rows, out_path.parent, research_root)

    source_name = rows[0]["source_name"] if rows else source_id
    html_doc = render_independent_html(
        rows,
        source_name=source_name,
        total=len(rows),
        analyzed_ok=result.analyzed_ok,
        analyzed_failed=result.analyzed_failed,
        unanalyzed=result.unanalyzed,
        research_rel_by_article=ctx["research_rel_by_article"],
        generated_at=datetime.now(timezone.utc),
        analysis_by_article=ctx["analysis_by_article"],
    )
    out_path.write_text(html_doc, encoding="utf-8")
    result.index_path = out_path
    result.files.append(out_path)
    logger.info("独立 HTML 导出完成: %d 篇 → %s", result.exported, out_path)
    return result


def export_portable_package(
    storage: Storage,
    out_dir: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int = 100,
    research_root: Optional[str | Path] = None,
) -> PortableResult:
    """导出「HTML 新闻包」：``out_dir/index.html`` + ``out_dir/articles/NNN.html``。

    输出结构（建议 ``data/export/portable/<site>-<date>/``）：:

        <site>-<date>/
        ├── index.html
        └── articles/
            ├── 001.html
            ├── 002.html
            └── ...

    ``index.html`` 为自包含阅读器；``articles/NNN.html`` 为单篇自包含页。
    整体复制到其它电脑后双击 ``index.html`` 即可阅读。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    articles_dir = out_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    rows = storage.list_articles_with_analysis(
        source_id=source_id, limit=limit if limit else 10**9
    )
    result = PortableResult()
    for row in rows:
        status = _ai_status(row)
        if status == "ok":
            result.analyzed_ok += 1
        elif status == "failed":
            result.analyzed_failed += 1
        else:
            result.unanalyzed += 1

    research_root = Path(research_root) if research_root else None
    ctx = _collect_context(storage, rows, out_dir, research_root)

    # article_id -> articles/NNN.html 相对路径（index 内跳转）
    article_pages: dict[int, str] = {}

    # 单篇页
    for i, row in enumerate(rows, start=1):
        article_id = int(row["id"])
        try:
            filename = f"{i:03d}.html"
            article_pages[article_id] = f"articles/{filename}"
            single_path = articles_dir / filename
            try:
                authors = json.loads(row["authors"] or "[]")
            except (ValueError, TypeError):
                authors = []
            status = _ai_status(row)
            html_doc = render_article_page(
                article_id=article_id,
                title=row["title"] or "",
                source_name=row["source_name"] or row["source_id"] or source_id,
                authors=authors,
                published_at=(
                    _parse_datetime(row["published_at"])
                    or _parse_datetime(row["discovered_at"])
                ),
                canonical_url=row["canonical_url"] or "",
                body_text=row["body_text"] or "",
                ai_status=status,
                analysis=ctx["analysis_by_article"].get(article_id, {}),
                index_rel="../index.html",
                research_rel="",
            )
            single_path.write_text(html_doc, encoding="utf-8")
            result.exported += 1
            result.files.append(single_path)
        except Exception as exc:
            result.failed += 1
            logger.error("便携单篇导出失败 #%s: %s", article_id, exc)

    # index.html —— 自包含阅读器（含单篇链接）
    source_name = rows[0]["source_name"] if rows else source_id
    html_doc = render_independent_html(
        rows,
        source_name=source_name,
        total=len(rows),
        analyzed_ok=result.analyzed_ok,
        analyzed_failed=result.analyzed_failed,
        unanalyzed=result.unanalyzed,
        research_rel_by_article=ctx["research_rel_by_article"],
        generated_at=datetime.now(timezone.utc),
        analysis_by_article=ctx["analysis_by_article"],
        article_pages=article_pages,
    )
    result.index_path = out_dir / "index.html"
    result.index_path.write_text(html_doc, encoding="utf-8")
    result.files.append(result.index_path)

    logger.info(
        "HTML 新闻包导出完成: %d 篇 → %s（已分析 %d / 失败 %d / 未分析 %d）",
        result.exported,
        out_dir,
        result.analyzed_ok,
        result.analyzed_failed,
        result.unanalyzed,
    )
    return result


def export_portable_reader_package(
    storage: Storage,
    out_dir: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int = 100,
    research_root: Optional[str | Path] = None,
) -> PortableResult:
    """导出「便携阅读包」：面向分享给其他电脑用户的阅读包。

    输出结构（建议 ``data/export/portable/Laxinwen-<site>-<date>/``）：::

        Laxinwen-<SITE>-<date>/
        ├── index.html          # 自包含阅读器（同 HTML 新闻包）
        ├── articles/           # 单篇自包含页
        │   ├── 001.html
        │   ├── 002.html
        │   └── ...
        ├── server.py           # 内嵌的迷你 HTTP 阅读服务器（纯标准库）
        └── Open-Reader.bat     # Windows 双击启动器

    与「HTML 新闻包」的区别：多出 ``server.py`` + ``Open-Reader.bat``，
    目标是**分享给其他电脑用户**：他们无需安装 laxinwen，双击一次
    ``Open-Reader.bat`` 即可通过 ``http://127.0.0.1:<port>/index.html``
    （而非 ``file://``）在浏览器中阅读，从而沉浸式翻译等浏览器扩展可正常工作。

    服务器只监听 127.0.0.1（不暴露到局域网）、自动选择空闲端口、
    不需要 SQLite / 互联网 / API Key；目标电脑只需有 Python 3（标准库）。
    若找不到 Python，Open-Reader.bat 会给出清晰提示（而非静默失败）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    articles_dir = out_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    rows = storage.list_articles_with_analysis(
        source_id=source_id, limit=limit if limit else 10**9
    )
    result = PortableResult()
    for row in rows:
        status = _ai_status(row)
        if status == "ok":
            result.analyzed_ok += 1
        elif status == "failed":
            result.analyzed_failed += 1
        else:
            result.unanalyzed += 1

    research_root = Path(research_root) if research_root else None
    ctx = _collect_context(storage, rows, out_dir, research_root)

    # article_id -> articles/NNN.html 相对路径（index 内跳转）
    article_pages: dict[int, str] = {}

    for i, row in enumerate(rows, start=1):
        article_id = int(row["id"])
        try:
            filename = f"{i:03d}.html"
            article_pages[article_id] = f"articles/{filename}"
            single_path = articles_dir / filename
            try:
                authors = json.loads(row["authors"] or "[]")
            except (ValueError, TypeError):
                authors = []
            status = _ai_status(row)
            html_doc = render_article_page(
                article_id=article_id,
                title=row["title"] or "",
                source_name=row["source_name"] or row["source_id"] or source_id,
                authors=authors,
                published_at=(
                    _parse_datetime(row["published_at"])
                    or _parse_datetime(row["discovered_at"])
                ),
                canonical_url=row["canonical_url"] or "",
                body_text=row["body_text"] or "",
                ai_status=status,
                analysis=ctx["analysis_by_article"].get(article_id, {}),
                index_rel="../index.html",
                research_rel="",
            )
            single_path.write_text(html_doc, encoding="utf-8")
            result.exported += 1
            result.files.append(single_path)
        except Exception as exc:
            result.failed += 1
            logger.error("便携阅读包单篇导出失败 #%s: %s", article_id, exc)

    # index.html —— 自包含阅读器（含单篇链接）
    source_name = rows[0]["source_name"] if rows else source_id
    html_doc = render_independent_html(
        rows,
        source_name=source_name,
        total=len(rows),
        analyzed_ok=result.analyzed_ok,
        analyzed_failed=result.analyzed_failed,
        unanalyzed=result.unanalyzed,
        research_rel_by_article=ctx["research_rel_by_article"],
        generated_at=datetime.now(timezone.utc),
        analysis_by_article=ctx["analysis_by_article"],
        article_pages=article_pages,
    )
    result.index_path = out_dir / "index.html"
    result.index_path.write_text(html_doc, encoding="utf-8")
    result.files.append(result.index_path)

    # server.py —— 内嵌的迷你本地 HTTP 阅读服务器（纯标准库）
    server_py = out_dir / "server.py"
    server_py.write_text(_PORTABLE_SERVER_PY, encoding="utf-8")
    result.files.append(server_py)

    # Open-Reader.bat —— Windows 双击启动器（不含 API Key / 绝对路径）
    bat = out_dir / "Open-Reader.bat"
    bat.write_text(_OPEN_READER_BAT, encoding="utf-8")
    result.files.append(bat)

    logger.info(
        "便携阅读包导出完成: %d 篇 → %s（已分析 %d / 失败 %d / 未分析 %d）",
        result.exported,
        out_dir,
        result.analyzed_ok,
        result.analyzed_failed,
        result.unanalyzed,
    )
    return result


def default_independent_path(source_id: str) -> Path:
    """默认独立 HTML 输出路径：``data/export/portable/<site>-<date>.html``。"""
    return Path("data") / "export" / "portable" / f"{source_id}-{datetime.now().strftime('%Y-%m-%d')}.html"


def default_package_path(source_id: str) -> Path:
    """默认 HTML 新闻包输出目录：``data/export/portable/<site>-<date>/``。"""
    return Path("data") / "export" / "portable" / f"{source_id}-{datetime.now().strftime('%Y-%m-%d')}"


def default_reader_path(source_id: str) -> Path:
    """默认便携阅读包输出目录：``data/export/portable/Laxinwen-<site>-<date>/``。"""
    return (
        Path("data")
        / "export"
        / "portable"
        / f"Laxinwen-{source_id.upper()}-{datetime.now().strftime('%Y-%m-%d')}"
    )
