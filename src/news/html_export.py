"""HTML 研究结果展示层 —— 把 SQLite 中已生成的 AI 分析结果渲染成适合人阅读的 HTML。

设计目标（对应"研究阅读页面"而非 SaaS Dashboard）：

- 纯 Python + HTML + CSS，HTML5 / UTF-8，中文与葡萄牙语重音字符正常显示；
- 不依赖外部 CDN / 字体 / JS：CSS 内嵌，无 JS 也能完整阅读，可直接双击打开；
- 每篇文章一个独立 HTML 文件，按 ``YYYY/MM/`` 组织；
- 同时生成 ``index.html`` 总索引（按日期倒序）；
- 只导出 ``status='ok'/'success'`` 的成功分析；失败文章不出现在正常研究页面，
  仅由 index.html 显示简单统计；
- 用户/文章内容全部 HTML escape，避免正文中的 HTML 破坏页面结构；
- 文件名使用安全 slug（不包含 Windows 非法字符）；
- 不修改 ``article_analysis`` schema，继续复用现有字段。

页面布局（自上而下）：

    来源徽标 + 标题
    原文信息（作者 / 发布日期 / 来源 / 原文链接）
    AI 中文摘要（summary_zh，完整显示不截断）
    关键观点（编号列表，key_points_json）
    主题（chip 标签，topics_json）
    实体（表格，entities_json，location 正常显示）
    市场相关性（HIGH / MEDIUM / LOW + 理由，明确标注"AI 判断，不代表原文事实"）
    原文语言
    AI Processing Metadata（provider / model / prompt_version / token usage / cost / created / updated）
    原文（"查看原文 →" 链接 + 可选原文正文，与 AI 分析明显分区）
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

# 合法实体类型（与 ai/prompts.py 一致）
VALID_ENTITY_TYPES = {
    "company",
    "person",
    "organization",
    "country",
    "location",
    "product",
}

# 市场相关性显示映射（数据库存小写）
_MARKET_RELEVANCE_LABEL = {
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
}

# 市场相关性 CSS class（用于着色）
_MARKET_RELEVANCE_CLASS = {
    "high": "mr-high",
    "medium": "mr-medium",
    "low": "mr-low",
}


@dataclass
class HtmlExportResult:
    """HTML 导出统计。"""

    exported: int = 0
    skipped: int = 0
    failed: int = 0
    analysis_ok: int = 0
    analysis_failed: int = 0
    files: list[Path] = field(default_factory=list)
    index_path: Optional[Path] = None

    def as_dict(self) -> dict:
        return {
            "exported": self.exported,
            "skipped": self.skipped,
            "failed": self.failed,
            "analysis_ok": self.analysis_ok,
            "analysis_failed": self.analysis_failed,
        }


def _e(value: Any) -> str:
    """HTML escape（把 None 安全转成空字符串）。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fmt_dt(value: Any) -> str:
    """把 ISO 时间字符串格式化为易读的 UTC 日期时间；空值返回 '—'。"""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return _e(value)


def _fmt_date(value: Any) -> str:
    """把 ISO 时间字符串格式化为 YYYY-MM-DD；空值返回 '—'。"""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return _e(value)


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


def _load_json(value: Any, default: Any = None) -> Any:
    """安全解析 JSON 字段（key_points_json / topics_json / entities_json / usage_json）。"""
    if value is None:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        logger.warning("JSON 字段解析失败，使用默认值: %r", str(value)[:80])
        return default if default is not None else []


def slugify(text: str, max_len: int = 60) -> str:
    """生成安全的文件名字段。

    - 移除 Windows 非法字符 ``\\ / : * ? \" < > |``；
    - 保留字母数字、CJK、拉丁扩展（含葡萄牙语重音）、``- _ .``；
    - 连续分隔符合并为单个 ``-``；
    - 空结果回退 ``article``。
    """
    norm = unicodedata.normalize("NFC", text or "")
    norm = re.sub(r'[\\/:*?"<>|]', " ", norm)
    norm = norm.replace(" ", "-")
    norm = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\u00c0-\u024f_\-.]", "-", norm)
    norm = re.sub(r"-+", "-", norm).strip("-.")
    norm = norm[:max_len].rstrip("-.")
    return norm or "article"


def _build_filename(article_id: Any, title: str, published: Any) -> str:
    """构建单篇 HTML 文件名：``<4位ID>-<slug>.html``。

    使用 canonical article id 前缀保证唯一性，标题 slug 保证可读性。
    """
    art_id = int(article_id) if article_id is not None else 0
    slug = slugify(title or "")
    return f"{art_id:04d}-{slug}.html"


# ---------- 单篇页面 ----------

_CSS = """
:root {
  --bg: #fafafa;
  --card-bg: #ffffff;
  --text: #1f2328;
  --text-muted: #57606a;
  --border: #d0d7de;
  --accent: #0969da;
  --accent-soft: #ddf4ff;
  --code-bg: #f6f8fa;
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
.page {
  max-width: 860px;
  margin: 0 auto;
  padding: 32px 20px 64px;
}
.card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 28px 32px;
  margin-bottom: 24px;
}
h1, h2, h3 { line-height: 1.3; }
h1 { font-size: 26px; margin: 8px 0 4px; }
h2 {
  font-size: 17px;
  margin: 0 0 12px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--accent-soft);
  color: #0a3069;
}
.breadcrumb {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 12px;
}
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
.meta {
  color: var(--text-muted);
  font-size: 14px;
  margin: 12px 0 0;
}
.meta table { border-collapse: collapse; }
.meta td { padding: 2px 16px 2px 0; vertical-align: top; }
.meta td:first-child { color: var(--text-muted); }
.meta a { color: var(--accent); word-break: break-all; }
.summary { white-space: pre-wrap; }
ol.key-points { padding-left: 24px; margin: 8px 0; }
ol.key-points li { margin-bottom: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-block;
  padding: 3px 12px;
  border-radius: 999px;
  font-size: 13px;
  background: #eaeef2;
  color: #1f2328;
  border: 1px solid var(--border);
}
table.entities { border-collapse: collapse; width: 100%; }
table.entities th, table.entities td {
  border: 1px solid var(--border);
  padding: 6px 12px;
  text-align: left;
  font-size: 14px;
}
table.entities th { background: var(--code-bg); }
.entity-type {
  display: inline-block;
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 6px;
  background: #fff8c5;
  color: #4d2d00;
  font-weight: 500;
}
.mr-badge {
  display: inline-block;
  font-size: 15px;
  font-weight: 700;
  padding: 4px 16px;
  border-radius: 8px;
  letter-spacing: 0.05em;
}
.mr-high { background: #ffebe9; color: #cf222e; border: 1px solid #ff8182; }
.mr-medium { background: #fff8c5; color: #7d4e00; border: 1px solid #d4a72c; }
.mr-low { background: #dafbe1; color: #116329; border: 1px solid #4ac26b; }
.mr-note {
  margin-top: 10px;
  font-size: 13px;
  color: var(--text-muted);
}
.reason {
  margin-top: 12px;
  padding: 12px 16px;
  background: var(--code-bg);
  border-left: 3px solid var(--border);
  border-radius: 0 6px 6px 0;
  font-size: 14px;
}
.language { font-size: 14px; }
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 4px 24px;
  font-size: 14px;
}
.meta-grid .k { color: var(--text-muted); }
.meta-grid .v { word-break: break-all; }
.usage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 4px 24px;
  font-size: 14px;
}
.usage-grid .k { color: var(--text-muted); }
.usage-grid .v { font-variant-numeric: tabular-nums; }
.original {
  border-top: 1px dashed var(--border);
  margin-top: 20px;
  padding-top: 16px;
}
.original h3 { font-size: 15px; color: var(--text-muted); margin: 0 0 8px; }
.btn-original {
  display: inline-block;
  padding: 8px 20px;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  text-decoration: none;
  font-weight: 600;
  font-size: 14px;
}
.btn-original:hover { background: #0550ae; }
.original-body {
  margin-top: 16px;
  padding: 16px;
  background: var(--code-bg);
  border-radius: 8px;
  max-height: 480px;
  overflow: auto;
  white-space: pre-wrap;
  font-size: 14px;
  color: var(--text);
}
.footnote {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted);
}
.footer {
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
  margin-top: 32px;
}
@media (max-width: 640px) {
  .page { padding: 16px 10px 40px; }
  .card { padding: 18px 16px; }
}
"""


# index.html 专用样式
_INDEX_CSS = """
.page { max-width: 960px; }
.entry {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 24px;
  margin-bottom: 16px;
}
.entry-meta { display: flex; gap: 16px; align-items: center; margin-bottom: 4px; }
.entry-date { font-size: 13px; color: var(--text-muted); }
.entry-source {
  font-size: 12px; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase;
  background: var(--accent-soft); color: #0a3069;
  padding: 2px 10px; border-radius: 999px;
}
.entry-title { margin: 8px 0 6px; font-size: 19px; }
.entry-title a { color: var(--text); text-decoration: none; }
.entry-title a:hover { color: var(--accent); text-decoration: underline; }
.entry-summary { color: var(--text); font-size: 14px; margin: 4px 0; }
.entry-meta2 { display: flex; gap: 16px; align-items: center; font-size: 13px; color: var(--text-muted); margin: 8px 0; }
.entry-model { color: var(--text-muted); }
.btn-small { padding: 4px 14px; font-size: 13px; }
.stats { display: flex; gap: 24px; margin: 16px 0; }
.stat {
  display: inline-flex; align-items: baseline; gap: 8px;
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 18px;
}
.stat-num { font-size: 22px; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 13px; color: var(--text-muted); }
"""


def _esc_json_list(items: list[str]) -> list[str]:
    """把 JSON 列表转成 HTML escape 后的字符串列表。"""
    out: list[str] = []
    for item in items or []:
        if item is None:
            continue
        out.append(_e(item))
    return out


def _render_entities(entities: list[dict]) -> str:
    """渲染实体表格。location 等合法类型正常显示，未知类型原样显示。"""
    if not entities:
        return '<p class="muted">（无实体）</p>'
    rows: list[str] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        name = _e(ent.get("name") or "")
        etype = _e(ent.get("type") or "—")
        rows.append(
            f"<tr><td>{name}</td><td><span class=\"entity-type\">{etype}</span></td></tr>"
        )
    if not rows:
        return '<p class="muted">（无实体）</p>'
    return (
        '<table class="entities"><thead><tr><th>实体</th><th>类型</th></tr></thead>'
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_usage(usage: dict) -> tuple[str, Optional[str]]:
    """渲染 token usage 与 cost。

    usage 可能来自两种存储形态：
    1. ``usage_json`` 字段（含 prompt_tokens / completion_tokens / total_tokens / cost / credit）；
    2. 旧版独立列（prompt_tokens / completion_tokens / total_tokens / cost）。
    返回 (usage_html, cost_html)。
    """
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    cost = usage.get("cost", usage.get("credit"))

    def _num(v: Any) -> str:
        if v is None:
            return "—"
        try:
            n = float(v)
            if n.is_integer():
                return str(int(n))
            return f"{n:g}"
        except (ValueError, TypeError):
            return _e(v)

    lines = [
        f"<div class=\"usage-grid\"><span class=\"k\">Prompt tokens:</span>"
        f"<span class=\"v\">{_num(prompt)}</span></div>",
        f"<div class=\"usage-grid\"><span class=\"k\">Completion tokens:</span>"
        f"<span class=\"v\">{_num(completion)}</span></div>",
        f"<div class=\"usage-grid\"><span class=\"k\">Total tokens:</span>"
        f"<span class=\"v\">{_num(total)}</span></div>",
    ]
    cost_html: Optional[str] = None
    if cost is not None:
        try:
            cost_val = float(cost)
            cost_html = f"{cost_val:.6f}"
        except (ValueError, TypeError):
            cost_html = _e(cost)
    return "".join(lines), cost_html


def _market_badge(relevance: str) -> str:
    rel = (relevance or "").strip().lower()
    label = _MARKET_RELEVANCE_LABEL.get(rel, (relevance or "—").upper())
    cls = _MARKET_RELEVANCE_CLASS.get(rel, "mr-medium")
    return f'<span class="mr-badge {cls}">{_e(label)}</span>'


def render_article_html(
    *,
    article_id: int,
    title: str,
    source_name: str,
    authors: list[str],
    published_at: Any,
    canonical_url: str,
    body_text: str,
    analysis: dict[str, Any],
    usage: dict[str, Any],
    cost: Optional[Any],
    created_at: Any,
    updated_at: Any,
    language: str,
    index_rel: str = "index.html",
    article_language: str = "",
) -> str:
    """渲染单篇研究阅读页面 HTML。"""
    a = analysis

    # 摘要（完整显示，不截断）
    summary = a.get("summary_zh") or ""

    # 关键观点
    key_points = _esc_json_list(a.get("key_points") or [])
    kp_html = ""
    if key_points:
        items = "".join(f"<li>{kp}</li>" for kp in key_points)
        kp_html = f'<ol class="key-points">{items}</ol>'
    else:
        kp_html = '<p class="muted">（无）</p>'

    # 主题 chips
    topics = _esc_json_list(a.get("topics") or [])
    topics_html = ""
    if topics:
        chips = "".join(f'<span class="chip">{t}</span>' for t in topics)
        topics_html = f'<div class="chips">{chips}</div>'
    else:
        topics_html = '<p class="muted">（无）</p>'

    # 实体表格
    entities_html = _render_entities(a.get("entities") or [])

    # 市场相关性
    relevance = a.get("market_relevance") or ""
    reason = a.get("market_relevance_reason") or ""
    market_html = _market_badge(relevance)
    reason_html = (
        f'<div class="reason"><strong>分析理由：</strong><br>{_e(reason)}</div>'
        if reason
        else '<p class="muted">（无理由说明）</p>'
    )

    # 原文语言
    detected_lang = a.get("language") or ""
    if detected_lang:
        lang_display = _e(detected_lang)
        if article_language and article_language.lower() != detected_lang.lower():
            lang_display += f" · 原文语言 {_e(article_language)}"
    else:
        lang_display = "—"

    # AI Processing Metadata
    if cost is not None and not usage.get("cost") and not usage.get("credit"):
        usage = {**usage, "cost": cost}
    usage_html, cost_html = _render_usage(usage)
    cost_line = f'<div class="usage-grid"><span class="k">Cost:</span><span class="v">{_e(cost_html)}</span></div>' if cost_html else ""

    # 原文
    original_body_html = ""
    if body_text and body_text.strip():
        original_body_html = (
            '<details><summary>显示原文正文（与 AI 分析分区展示）</summary>'
            f'<div class="original-body">{_e(body_text)}</div></details>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · AI 研究分析 · laxinwen</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">

  <div class="breadcrumb"><a href="{_e(index_rel)}">← 返回全部研究结果</a></div>

  <div class="card">
    <span class="site-badge">{_e(source_name)}</span>
    <h1>{_e(title)}</h1>
    <div class="meta">
      <table>
        <tr><td>作者：</td><td>{_e(', '.join(authors)) if authors else '—'}</td></tr>
        <tr><td>发布日期：</td><td>{_fmt_date(published_at)}</td></tr>
        <tr><td>来源：</td><td>{_e(source_name)}</td></tr>
        <tr><td>原文链接：</td><td><a href="{_e(canonical_url)}" rel="noopener" target="_blank">{_e(canonical_url)}</a></td></tr>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>AI 中文摘要</h2>
    <div class="summary">{_e(summary)}</div>
    <p class="footnote">数据来源：summary_zh</p>
  </div>

  <div class="card">
    <h2>关键观点</h2>
    {kp_html}
    <p class="footnote">数据来源：key_points_json</p>
  </div>

  <div class="card">
    <h2>主题</h2>
    {topics_html}
    <p class="footnote">数据来源：topics_json</p>
  </div>

  <div class="card">
    <h2>实体</h2>
    {entities_html}
    <p class="footnote">数据来源：entities_json</p>
  </div>

  <div class="card">
    <h2>市场相关性</h2>
    <p>{market_html} <span class="mr-note">（AI 判断，不代表原文事实。）</span></p>
    {reason_html}
    <p class="footnote">数据来源：market_relevance / market_relevance_reason</p>
  </div>

  <div class="card">
    <h2>原文语言</h2>
    <p class="language">{lang_display}</p>
  </div>

  <div class="card">
    <h2>AI Processing Metadata</h2>
    <div class="meta-grid">
      <div><span class="k">Provider:</span><br><span class="v">{_e(a.get('provider'))}</span></div>
      <div><span class="k">Model:</span><br><span class="v">{_e(a.get('model'))}</span></div>
      <div><span class="k">Prompt Version:</span><br><span class="v">{_e(a.get('prompt_version'))}</span></div>
    </div>
    <h3 style="font-size:14px;color:var(--text-muted);margin:16px 0 8px;">Token Usage</h3>
    {usage_html}
    {cost_line}
    <h3 style="font-size:14px;color:var(--text-muted);margin:16px 0 8px;">时间</h3>
    <div class="meta-grid">
      <div><span class="k">Created:</span><br><span class="v">{_fmt_dt(created_at)}</span></div>
      <div><span class="k">Updated:</span><br><span class="v">{_fmt_dt(updated_at)}</span></div>
    </div>
  </div>

  <div class="card">
    <h2>原文</h2>
    <p><a class="btn-original" href="{_e(canonical_url)}" rel="noopener" target="_blank">查看原文 →</a></p>
    <div class="original">
      <h3>原文正文（独立分区，不混入 AI 分析）</h3>
      {original_body_html}
    </div>
  </div>

  <div class="footer">
    由 laxinwen 生成 · AI 分析结果仅供研究参考，不构成投资建议
  </div>
</div>
</body>
</html>
"""


# ---------- index.html ----------

def _index_summary(summary_zh: str, limit: int = 260) -> str:
    """截取中文摘要前 N 字作为索引展示（保留完整句子边界优先）。"""
    text = (summary_zh or "").strip()
    if not text:
        return "（无摘要）"
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 尽量在标点处截断
    for sep in ("。", "；", "！", "？", "\n", "，", " "):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            cut = cut[: idx + 1]
            break
    return cut + "……"


def _index_rows(rows: list, rel_by_article: dict[int, str]) -> list[dict]:
    """把 SQLite 行转成 index 渲染所需 dict，按发布日期倒序。"""
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        published = _parse_datetime(row.get("published_at")) or _parse_datetime(row.get("discovered_at"))
        out.append(
            {
                "date": _fmt_date(published),
                "published_sort": published.isoformat() if published else "",
                "source": row.get("source_name") or row.get("art_source_id") or "—",
                "title": row.get("art_title") or "",
                "summary": row.get("summary_zh") or "",
                "relevance": (row.get("market_relevance") or "").strip().lower(),
                "model": row.get("model") or "—",
                "rel_file": rel_by_article.get(int(row.get("article_id") or 0), ""),
                "article_id": row.get("article_id"),
            }
        )
    out.sort(key=lambda d: d["published_sort"], reverse=True)
    return out


def _render_index_html(
    rows: list,
    *,
    analysis_ok: int,
    analysis_failed: int,
    rel_by_article: dict[int, str] | None = None,
) -> str:
    """渲染 index.html 总索引（按日期倒序 + 简单统计，无 JS）。"""
    rel_by_article = rel_by_article or {}
    items = _index_rows(rows, rel_by_article)
    cards: list[str] = []
    for it in items:
        rel = _MARKET_RELEVANCE_LABEL.get(it["relevance"], (it["relevance"] or "—").upper())
        cls = _MARKET_RELEVANCE_CLASS.get(it["relevance"], "mr-medium")
        summary = _index_summary(it["summary"])
        cards.append(
            f"""<article class="entry">
  <div class="entry-meta">
    <span class="entry-date">{_e(it['date'])}</span>
    <span class="entry-source">{_e(it['source'])}</span>
  </div>
  <h3 class="entry-title"><a href="{_e(it['rel_file'])}">{_e(it['title'])}</a></h3>
  <p class="entry-summary">中文摘要：{_e(summary)}</p>
  <p class="entry-meta2">
    市场相关性：<span class="mr-badge {cls}">{_e(rel)}</span>
    <span class="entry-model">AI model：{_e(it['model'])}</span>
  </p>
  <p><a class="btn-original btn-small" href="{_e(it['rel_file'])}">阅读分析 →</a></p>
</article>"""
        )
    entries_html = "".join(cards) if cards else '<p class="muted">（暂无成功分析的 AI 研究结果）</p>'

    stat_html = (
        f'<div class="stat"><span class="stat-num">{analysis_ok}</span><span class="stat-label">成功</span></div>'
        f'<div class="stat"><span class="stat-num">{analysis_failed}</span><span class="stat-label">失败</span></div>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 研究结果 · laxinwen</title>
<style>{_CSS}{_INDEX_CSS}</style>
</head>
<body>
<div class="page">

  <div class="card">
    <h1>AI 研究结果</h1>
    <p class="meta">所有已成功完成 AI 分析的新闻文章，按日期倒序排列。点击“阅读分析”进入研究阅读页面。</p>
    <div class="stats">
      <div class="stat"><span class="stat-num">{analysis_ok}</span><span class="stat-label">成功</span></div>
      <div class="stat"><span class="stat-num">{analysis_failed}</span><span class="stat-label">失败</span></div>
    </div>
    <p class="footnote">AI 分析统计：成功 {analysis_ok} / 失败 {analysis_failed}。失败记录不会出现在正常研究结果中。</p>
  </div>

  {entries_html}

  <div class="footer">由 laxinwen 生成 · AI 分析结果仅供研究参考，不构成投资建议</div>
</div>
</body>
</html>
"""


# ---------- 顶层导出 ----------

def export_html(
    storage: Storage,
    out_dir: str | Path,
    *,
    source_id: Optional[str] = None,
    article_id: Optional[int] = None,
) -> HtmlExportResult:
    """导出 AI 分析研究结果 HTML。

    默认导出所有 status='ok'/'success' 的成功分析；
    ``source_id`` 只导出指定站点；``article_id`` 只导出指定文章。

    输出结构：:

        out_dir/
        ├── index.html
        └── YYYY/MM/0001-<slug>.html

    返回 HtmlExportResult（含 exported/skipped/failed 与统计）。
    """
    out_dir = Path(out_dir)
    root = out_dir
    root.mkdir(parents=True, exist_ok=True)

    rows = storage.list_analysis_success(
        source_id=source_id,
        article_id=article_id,
        limit=10**9,
    )
    result = HtmlExportResult()
    result.analysis_ok = len(rows)
    result.analysis_failed = storage.count_analysis(status="failed", source_id=source_id)

    # article_id -> 相对路径（YYYY/MM/xxxx-slug.html），供 index.html 链接使用
    rel_by_article: dict[int, str] = {}
    for row in rows:
        try:
            rel = _export_one(root, row)
            result.exported += 1
            result.files.append(rel)
            rel_by_article[int(row["article_id"])] = rel.as_posix()
        except Exception as exc:
            result.failed += 1
            logger.error("HTML 导出失败 #%s: %s", row.get("article_id"), exc)

    # index.html 总索引（按日期倒序 + 简单统计）
    # 局部导出（--site / --article-id）时同样生成 index.html，便于在该导出目录直接预览。
    result.index_path = root / "index.html"
    index_html = _render_index_html(
        rows,
        analysis_ok=result.analysis_ok,
        analysis_failed=result.analysis_failed,
        rel_by_article=rel_by_article,
    )
    result.index_path.write_text(index_html, encoding="utf-8")

    result.skipped = max(0, len(rows) - result.exported)
    logger.info("HTML 导出完成: %d 篇 → %s", result.exported, out_dir)
    return result


def _export_one(root: Path, row: sqlite3.Row) -> Path:
    """导出单篇文章 HTML，返回相对路径（如 ``2026/08/0001-xxx.html``）。"""
    article_id = int(row["article_id"])
    title = row["art_title"] or ""
    published = _parse_datetime(row["published_at"]) or _parse_datetime(row["discovered_at"])
    if published:
        month_dir = root / f"{published:%Y}" / f"{published:%m}"
    else:
        month_dir = root / "unknown" / "00"

    filename = _build_filename(article_id, title, published)
    month_dir.mkdir(parents=True, exist_ok=True)

    # usage 解析：全部来自 usage_json（该分支 schema 无独立 token/cost 列）
    usage = _load_json(row["usage_json"], {})
    if not isinstance(usage, dict):
        usage = {}

    cost = usage.get("cost", usage.get("credit"))

    analysis = {
        "summary_zh": row["summary_zh"] or "",
        "key_points": _load_json(row["key_points_json"], []),
        "topics": _load_json(row["topics_json"], []),
        "entities": _load_json(row["entities_json"], []),
        "market_relevance": row["market_relevance"] or "",
        "market_relevance_reason": row["market_relevance_reason"] or "",
        "language": row["language"] or "",
        "provider": row["provider"] or "",
        "model": row["model"] or "",
        "prompt_version": row["prompt_version"] or "",
    }

    body_text = row["body_text"] or ""
    import json as _json

    try:
        authors = _json.loads(row["art_authors"] or "[]")
    except (ValueError, TypeError):
        authors = []
    html_doc = render_article_html(
        article_id=article_id,
        title=title,
        source_name=row["source_name"] or row["art_source_id"] or "",
        authors=authors,
        published_at=published,
        canonical_url=row["canonical_url"] or "",
        body_text=body_text,
        analysis=analysis,
        usage=usage,
        cost=cost,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        language=analysis["language"],
        index_rel="../../index.html",
        article_language=row["art_language"] or "",
    )
    month_dir.joinpath(filename).write_text(html_doc, encoding="utf-8")
    # 返回相对路径（YYYY/MM/filename.html），供 index.html 链接使用
    return Path(f"{month_dir.relative_to(root).as_posix()}/{filename}")
