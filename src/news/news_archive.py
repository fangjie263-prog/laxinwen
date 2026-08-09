"""News Archive HTML —— 最近 N 条新闻的阅读目录。

与 ``html_export.py``（AI Research HTML）语义明确分开：

    SQLite
      ↓
    News Archive HTML（news-html）：直接读取 articles 表，不要求 AI 成功
      ↓ 每篇可链接到
    AI Research HTML（html）：只显示 AI 分析成功的文章

设计目标（对应"最近 N 条新闻阅读目录"而非 Dashboard）：

- 纯 Python + HTML + CSS，HTML5 / UTF-8，中文与葡萄牙语重音字符正常显示；
- 不依赖外部 CDN / 字体 / JS：CSS 内嵌，无 JS 也能完整阅读，可直接双击打开；
- 每篇文章一个独立 HTML 文件，按 ``YYYY/MM/`` 组织；
- 同时生成 ``index.html`` 总索引（按发布日期倒序）；
- 每条新闻显示：日期 / 时间 / 来源 / 标题 / 作者 / 中文摘要（如有） / AI 状态 / 原文链接 / AI Research 链接（如有成功分析）；
- AI 状态明确三态：✓ 已分析 / ⚠ 失败 / ○ 未分析，绝不把失败伪装成成功；
- 用户/文章内容全部 HTML escape；
- 不修改 article_analysis schema，继续复用现有字段。
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

from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class NewsArchiveResult:
    """News Archive HTML 导出统计。"""

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


# ---------- 工具 ----------

def _e(value: Any) -> str:
    """HTML escape（把 None 安全转成空字符串）。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _parse_datetime(value: Any) -> Optional[datetime]:
    """把 ISO 时间字符串解析为带 UTC 时区的 datetime；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _fmt_dt(value: Any) -> str:
    """把 ISO 时间字符串格式化为 YYYY-MM-DD HH:MM（UTC）；空值返回 '—'。"""
    dt = _parse_datetime(value)
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_date(value: Any) -> str:
    """把 ISO 时间字符串格式化为 YYYY-MM-DD；空值返回 '—'。"""
    dt = _parse_datetime(value)
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d")


def _load_json(value: Any, default: Any = None) -> Any:
    """安全解析 JSON 字段。"""
    if value is None:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        return default if default is not None else []


def slugify(text: str, max_len: int = 60) -> str:
    """生成安全的文件名字段（与 html_export 保持一致）。"""
    norm = unicodedata.normalize("NFC", text or "")
    norm = re.sub(r'[\\/:*?"<>|]', " ", norm)
    norm = norm.replace(" ", "-")
    norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\u00c0-\u024f_\-.]", "-", norm)
    norm = re.sub(r"-+", "-", norm).strip("-.")
    norm = norm[:max_len].rstrip("-.")
    return norm or "article"


def _build_filename(article_id: Any, title: str, published: Any) -> str:
    """构建单篇 HTML 文件名：``<4位ID>-<slug>.html``。"""
    art_id = int(article_id) if article_id is not None else 0
    slug = slugify(title or "")
    return f"{art_id:04d}-{slug}.html"


# ---------- AI 状态 ----------

def _ai_status(row: sqlite3.Row) -> str:
    """返回文章 AI 状态：'ok' / 'failed' / 'none'。

    优先看成功分析；若无成功记录，再看是否有失败分析。
    """
    if isinstance(row, sqlite3.Row):
        row = dict(row)
    if row.get("ai_status"):
        return "ok"
    # 没有成功 → 查是否有失败记录
    has_failed = row.get("ai_has_failed")
    if has_failed:
        return "failed"
    return "none"


def _ai_status_html(status: str) -> str:
    """渲染 AI 状态徽标（✓ 已分析 / ⚠ 失败 / ○ 未分析）。"""
    if status == "ok":
        return '<span class="ai-badge ai-ok">✓ AI 已分析</span>'
    if status == "failed":
        return '<span class="ai-badge ai-failed">⚠ AI 分析失败</span>'
    return '<span class="ai-badge ai-none">○ 尚未分析</span>'


# ---------- CSS ----------

_CSS = """
:root {
  --bg: #fafafa;
  --card-bg: #ffffff;
  --text: #1f2328;
  --text-muted: #57606a;
  --border: #d0d7de;
  --accent: #0969da;
  --accent-soft: #ddf4ff;
  --ok: #1a7f37;
  --ok-soft: #dafbe1;
  --failed: #cf222e;
  --failed-soft: #ffebe9;
  --none-soft: #eaeef2;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Microsoft YaHei", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif;
  line-height: 1.7;
  font-size: 16px;
}
.page { max-width: 900px; margin: 0 auto; padding: 32px 20px 64px; }
.card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 24px 28px;
  margin-bottom: 20px;
}
h1 { font-size: 26px; margin: 8px 0 4px; line-height: 1.3; }
h2 { font-size: 17px; margin: 0 0 12px; padding-bottom: 8px; border-bottom: 2px solid var(--accent-soft); color: #0a3069; }
h3 { font-size: 15px; margin: 4px 0 8px; line-height: 1.4; }
.meta { color: var(--text-muted); font-size: 14px; margin: 8px 0 0; }
.breadcrumb { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; }
.breadcrumb a { color: var(--accent); text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.site-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: #0a3069;
  text-transform: uppercase;
}

/* 条目（index 列表） */
.entry {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 24px;
  margin-bottom: 14px;
}
.entry-meta { font-size: 13px; color: var(--text-muted); display: flex; gap: 14px; flex-wrap: wrap; align-items: center; }
.entry-title { font-size: 17px; margin: 6px 0 6px; }
.entry-title a { color: var(--text); text-decoration: none; }
.entry-title a:hover { color: var(--accent); text-decoration: underline; }
.entry-author { font-size: 13px; color: var(--text-muted); }
.entry-summary {
  font-size: 14px;
  color: var(--text-muted);
  background: #f6f8fa;
  border-left: 3px solid var(--accent-soft);
  padding: 8px 12px;
  margin: 8px 0;
  white-space: pre-wrap;
}
.entry-actions { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
.entry-source-link { font-size: 13px; }

/* AI 徽标 */
.ai-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
}
.ai-ok { background: var(--ok-soft); color: var(--ok); }
.ai-failed { background: var(--failed-soft); color: var(--failed); }
.ai-none { background: var(--none-soft); color: var(--text-muted); }

/* 按钮 */
.btn {
  display: inline-block;
  font-size: 13px;
  font-weight: 600;
  padding: 5px 14px;
  border-radius: 6px;
  text-decoration: none;
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { opacity: 0.9; }
.btn-outline { border: 1px solid var(--border); color: var(--accent); background: var(--card-bg); }
.btn-outline:hover { background: var(--accent-soft); }

/* 单篇页 */
.meta table { border-collapse: collapse; }
.meta td { padding: 2px 16px 2px 0; vertical-align: top; }
.meta td:first-child { color: var(--text-muted); }
.meta a { color: var(--accent); word-break: break-all; }
.summary { white-space: pre-wrap; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-block;
  padding: 3px 12px;
  border-radius: 999px;
  font-size: 13px;
  background: var(--accent-soft);
  color: #0a3069;
}
.entity-table { border-collapse: collapse; width: 100%; }
.entity-table th, .entity-table td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; font-size: 14px; }
.entity-table th { background: #f6f8fa; }
.original-body {
  background: #f6f8fa;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 16px;
  white-space: pre-wrap;
  font-size: 14px;
  margin-top: 8px;
}
.mr-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 700;
  padding: 2px 10px;
  border-radius: 999px;
}
.mr-high { background: #ffebe9; color: #cf222e; }
.mr-medium { background: #fff8c5; color: #7d4e00; }
.mr-low { background: var(--ok-soft); color: var(--ok); }
.notice { color: var(--text-muted); font-size: 14px; }
.footer { color: var(--text-muted); font-size: 13px; text-align: center; margin-top: 24px; }
"""


# ---------- 单篇页面 ----------

def render_article_page(
    *,
    article_id: int,
    title: str,
    source_name: str,
    authors: list[str],
    published_at: Any,
    canonical_url: str,
    body_text: str,
    ai_status: str,
    analysis: dict[str, Any],
    index_rel: str = "index.html",
    research_rel: str = "",
) -> str:
    """渲染单篇 News Archive 页面。

    有 AI 成功分析 → 显示 AI 详情（摘要/关键观点/主题/实体/市场相关性/理由/语言）；
    无成功分析 → 显示"尚未进行 AI 分析"或"AI 分析失败"，随后显示原文。
    """
    has_ai = ai_status == "ok"

    # AI 详情区块
    ai_section = ""
    if has_ai:
        summary = analysis.get("summary_zh") or ""
        key_points = analysis.get("key_points") or []
        topics = analysis.get("topics") or []
        entities = analysis.get("entities") or []
        relevance = (analysis.get("market_relevance") or "").strip().lower()
        reason = analysis.get("market_relevance_reason") or ""
        detected_lang = analysis.get("language") or ""

        kp_html = "".join(f"<li>{_e(k)}</li>" for k in key_points)
        kp_html = f'<ol style="padding-left:24px;">{kp_html}</ol>' if kp_html else '<p class="notice">（无）</p>'
        chips = "".join(f'<span class="chip">{_e(t)}</span>' for t in topics)
        topics_html = f'<div class="chips">{chips}</div>' if chips else '<p class="notice">（无）</p>'

        rows_html = ""
        for ent in entities:
            if isinstance(ent, dict):
                rows_html += (
                    f"<tr><td>{_e(ent.get('name'))}</td><td>{_e(ent.get('type'))}</td></tr>"
                )
        entities_html = (
            '<table class="entity-table"><tr><th>实体</th><th>类型</th></tr>'
            f"{rows_html}</table>"
            if rows_html
            else '<p class="notice">（无）</p>'
        )

        mr_cls = {"high": "mr-high", "medium": "mr-medium", "low": "mr-low"}.get(relevance, "mr-medium")
        mr_label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(relevance, (relevance or "—").upper())
        reason_html = (
            f'<p class="notice"><strong>分析理由：</strong> {_e(reason)}</p>'
            if reason
            else ""
        )

        ai_section = f"""
  <div class="card">
    <h2>AI 分析</h2>
    <p>{_ai_status_html('ok')}</p>
    <h3>中文摘要</h3>
    <div class="summary">{_e(summary)}</div>
    <h3>关键观点</h3>
    {kp_html}
    <h3>主题</h3>
    {topics_html}
    <h3>实体</h3>
    {entities_html}
    <h3>市场相关性</h3>
    <p><span class="mr-badge {mr_cls}">{_e(mr_label)}</span> <span class="notice">（AI 判断，不代表原文事实）</span></p>
    {reason_html}
    <h3>原文语言</h3>
    <p class="notice">{_e(detected_lang) if detected_lang else '—'}</p>
  </div>
"""
    else:
        status_text = (
            "<p class='notice'>该文章之前尝试进行 AI 分析但失败了（分析记录失败）。</p>"
            if ai_status == "failed"
            else "<p class='notice'>该文章尚未进行 AI 分析。运行 <code>news process</code> 可为其生成中文摘要与观点。</p>"
        )
        ai_section = f"""
  <div class="card">
    <h2>AI 分析</h2>
    {_ai_status_html(ai_status)}
    {status_text}
  </div>
"""

    # 原文正文
    body_html = ""
    if body_text and body_text.strip():
        body_html = f'<div class="original-body">{_e(body_text)}</div>'
    else:
        body_html = '<p class="notice">（暂无原文正文）</p>'

    research_link = ""
    if has_ai and research_rel:
        research_link = (
            f'<a class="btn btn-primary" href="{_e(research_rel)}" '
            f'rel="noopener" target="_blank">查看 AI Research →</a>'
        )
    original_link = (
        f'<a class="btn btn-outline" href="{_e(canonical_url)}" '
        f'rel="noopener" target="_blank">阅读原文 →</a>'
        if canonical_url
        else ""
    )
    action_bar = (
        f'<div class="entry-actions" style="margin-top:14px;">{research_link} {original_link}</div>'
        if (research_link or original_link)
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · News Archive · laxinwen</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">

  <div class="breadcrumb"><a href="{_e(index_rel)}">← 返回新闻列表</a></div>

  <div class="card">
    <span class="site-badge">{_e(source_name)}</span>
    <h1>{_e(title)}</h1>
    <div class="meta">
      <table>
        <tr><td>作者：</td><td>{_e(', '.join(authors)) if authors else '—'}</td></tr>
        <tr><td>发布日期：</td><td>{_e(_fmt_dt(published_at))}</td></tr>
        <tr><td>来源：</td><td>{_e(source_name)}</td></tr>
        <tr><td>原文链接：</td><td><a href="{_e(canonical_url)}" rel="noopener" target="_blank">{_e(canonical_url)}</a></td></tr>
        <tr><td>AI 状态：</td><td>{_ai_status_html(ai_status)}</td></tr>
      </table>
    </div>
    {action_bar}
  </div>

  {ai_section}

  <div class="card">
    <h2>原文正文</h2>
    {body_html}
  </div>

  <div class="footer">由 laxinwen 生成 · News Archive（最近 N 条新闻阅读目录）</div>
</div>
</body>
</html>
"""


# ---------- index.html ----------

def _index_summary(summary_zh: str, limit: int = 200) -> str:
    """截取中文摘要前 N 字作为索引展示。"""
    text = (summary_zh or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", "！", "？", "\n", "，", " "):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            cut = cut[: idx + 1]
            break
    return cut + "……"


def _render_index_html(
    rows: list,
    *,
    source_name: str,
    total: int,
    analyzed_ok: int,
    analyzed_failed: int,
    unanalyzed: int,
    rel_by_article: dict[int, str],
    research_rel_by_article: dict[int, str],
    generated_at: datetime,
) -> str:
    """渲染 News Archive 首页（最近 N 条，按发布日期倒序）。"""
    entries: list[str] = []
    for row in rows:
        r = dict(row)
        article_id = int(r["id"])
        title = r["title"] or ""
        published = _parse_datetime(r["published_at"]) or _parse_datetime(r["discovered_at"])
        dt_str = _fmt_dt(published) if published else "—"

        try:
            authors = json.loads(r["authors"] or "[]")
        except (ValueError, TypeError):
            authors = []
        author_str = ", ".join(authors) if authors else ""

        status = _ai_status(r)
        summary = r.get("summary_zh") or ""

        rel = rel_by_article.get(article_id, "")
        research_rel = research_rel_by_article.get(article_id, "")
        actions = f'<a class="btn btn-outline" href="{_e(rel)}">阅读全文 →</a>'
        if research_rel:
            actions += (
                f'<a class="btn btn-primary" href="{_e(research_rel)}" '
                f'rel="noopener" target="_blank">AI Research</a>'
            )

        summary_html = (
            f'<div class="entry-summary">中文摘要：{_e(summary)}</div>'
            if summary
            else ""
        )

        entries.append(
            f"""<article class="entry">
  <div class="entry-meta">
    <span>{_e(dt_str)}</span>
    <span>{_e(source_name)}</span>
    {_ai_status_html(status)}
  </div>
  <h3 class="entry-title"><a href="{_e(rel)}">{_e(title)}</a></h3>
  <div class="entry-author">{_e(author_str) if author_str else ''}</div>
  {summary_html}
  <div class="entry-actions">
    {actions}
    <span class="entry-source-link"><a href="{_e(r['canonical_url'] or '')}" rel="noopener" target="_blank">原文链接</a></span>
  </div>
</article>"""
        )

    entries_html = "".join(entries) if entries else '<p class="notice">（数据库暂无新闻）</p>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(source_name)} News Archive · laxinwen</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">

  <div class="card">
    <h1>{_e(source_name)}</h1>
    <p class="meta">News Archive · 最近 {total} 条新闻 · 更新时间 {generated_at.strftime('%Y-%m-%d %H:%M UTC')}</p>
    <p class="meta">按发布日期倒序排列。AI 状态：{_ai_status_html('ok')} / {_ai_status_html('failed')} / {_ai_status_html('none')}</p>
  </div>

  {entries_html}

  <div class="footer">由 laxinwen 生成 · News Archive（最近 N 条新闻阅读目录）</div>
</div>
</body>
</html>
"""


# ---------- 顶层导出 ----------

def export_news_archive(
    storage: Storage,
    out_dir: str | Path,
    *,
    source_id: Optional[str] = None,
    limit: int = 100,
) -> NewsArchiveResult:
    """导出 News Archive HTML（直接读取 articles 表，不要求 AI 成功）。

    输出结构：:

        out_dir/
        ├── index.html
        └── YYYY/MM/0001-<slug>.html

    返回 NewsArchiveResult。
    """
    out_dir = Path(out_dir)
    root = out_dir
    root.mkdir(parents=True, exist_ok=True)

    if not source_id:
        # 未指定站点时，仅当数据库只有一个站点时自动推断；否则需要 --site
        from .config import list_available_sites

        sites = list_available_sites()
        if len(sites) == 1:
            source_id = sites[0]
        else:
            raise ValueError(
                "News Archive 导出需要指定站点：--site <id>（数据库有多个站点）"
            )

    rows = storage.list_articles_with_analysis(source_id=source_id, limit=limit)
    result = NewsArchiveResult()

    # article_id -> 相对路径（YYYY/MM/xxxx-slug.html）
    rel_by_article: dict[int, str] = {}
    # article_id -> AI Research 相对路径（若存在成功分析，指向 data/export/html/ 对应页面）
    research_rel_by_article: dict[int, str] = {}

    # 查找 AI Research HTML 导出目录（默认 data/export/html/），建立 article_id → 研究页相对路径
    research_root = _locate_research_root(out_dir)
    if research_root is not None:
        # index.html 在 out_dir/（如 data/export/news-html/eco/），到 research_root（data/export/html/）
        # 用 os.path.relpath 计算正确相对层级（如 ../../html）
        import os as _os

        rel_prefix = _os.path.relpath(str(research_root), start=str(out_dir)).replace("\\", "/")
        for rrow in storage.list_analysis_success(source_id=source_id, limit=10**9):
            art_id = int(rrow["article_id"])
            pub = _parse_datetime(rrow["published_at"]) or _parse_datetime(rrow["discovered_at"])
            fname = _build_filename(art_id, rrow["art_title"] or "", pub)
            month = f"{pub:%Y}/{pub:%m}" if pub else "unknown/00"
            research_rel_by_article[art_id] = f"{rel_prefix}/{month}/{fname}"

    for row in rows:
        article_id = int(row["id"])
        try:
            rel = _export_one_article(
                root,
                row,
                storage=storage,
                research_rel=research_rel_by_article.get(article_id, ""),
            )
            result.exported += 1
            result.files.append(rel)
            rel_by_article[article_id] = rel.as_posix()
            status = _ai_status(row)
            if status == "ok":
                result.analyzed_ok += 1
            elif status == "failed":
                result.analyzed_failed += 1
            else:
                result.unanalyzed += 1
        except Exception as exc:
            result.failed += 1
            logger.error("News Archive 导出失败 #%s: %s", article_id, exc)

    # index.html
    result.index_path = root / "index.html"
    index_html = _render_index_html(
        rows,
        source_name=rows[0]["source_name"] if rows else source_id,
        total=len(rows),
        analyzed_ok=result.analyzed_ok,
        analyzed_failed=result.analyzed_failed,
        unanalyzed=result.unanalyzed,
        rel_by_article=rel_by_article,
        research_rel_by_article=research_rel_by_article,
        generated_at=datetime.now(timezone.utc),
    )
    result.index_path.write_text(index_html, encoding="utf-8")

    logger.info(
        "News Archive 导出完成: %d 篇 → %s（已分析 %d / 失败 %d / 未分析 %d）",
        result.exported,
        out_dir,
        result.analyzed_ok,
        result.analyzed_failed,
        result.unanalyzed,
    )
    return result


def _locate_research_root(out_dir: Path) -> Optional[Path]:
    """定位 AI Research HTML 导出目录（默认 data/export/html/）。

    News Archive 默认输出到 ``data/export/news-html/<site>/``，
    其兄弟目录 ``data/export/html/`` 存放 AI Research 结果。
    若不存在则不返回（单篇页不显示 Research 链接）。
    """
    candidates = [
        out_dir.parent / "html",          # data/export/news-html/<site>/../html → data/export/html
        out_dir.parents[1] / "html",      # data/export/html（若 out_dir 是 <site> 目录）
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _export_one_article(
    root: Path,
    row: sqlite3.Row,
    storage: Storage | None = None,
    research_rel: str = "",
) -> Path:
    """导出单篇文章 News Archive 页面，返回相对路径（如 ``2026/08/0001-xxx.html``）。

    ``research_rel``：从当前单篇页到 AI Research 对应页面的相对路径（如有成功分析）。
    若传入的是 index 级相对路径（如 ``../../html/...``），则根据单篇页目录自动修正层级。
    """
    article_id = int(row["id"])
    title = row["title"] or ""
    published = _parse_datetime(row["published_at"]) or _parse_datetime(row["discovered_at"])
    if published:
        month_dir = root / f"{published:%Y}" / f"{published:%m}"
    else:
        month_dir = root / "unknown" / "00"

    filename = _build_filename(article_id, title, published)
    month_dir.mkdir(parents=True, exist_ok=True)

    # research_rel 传入的是 index 级路径（../../html/...），单篇页在 root/YYYY/MM/ 下深 2 级，需补 2 个 ../
    if research_rel and research_rel.startswith("../"):
        research_rel = f"../../{research_rel}"

    try:
        authors = json.loads(row["authors"] or "[]")
    except (ValueError, TypeError):
        authors = []

    status = _ai_status(row)
    # 若有成功分析，构造 analysis dict（供单篇页 AI 区块展示完整详情）
    analysis: dict[str, Any] = {}
    if status == "ok" and storage is not None:
        arow = storage.get_analysis_for_article(article_id)
        if arow is not None:
            analysis = {
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

    html_doc = render_article_page(
        article_id=article_id,
        title=title,
        source_name=row["source_name"] or row["source_id"] or "",
        authors=authors,
        published_at=published,
        canonical_url=row["canonical_url"] or "",
        body_text=row["body_text"] or "",
        ai_status=status,
        analysis=analysis,
        index_rel="../../index.html",
        research_rel=research_rel,
    )
    month_dir.joinpath(filename).write_text(html_doc, encoding="utf-8")
    return Path(f"{month_dir.relative_to(root).as_posix()}/{filename}")
