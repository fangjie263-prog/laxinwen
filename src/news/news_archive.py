"""News Archive HTML —— 最近 N 条新闻的 Daily Reader 阅读器。

与 ``html_export.py``（AI Research HTML）语义明确分开：

    SQLite
      ↓
    News Archive HTML（news-html）：直接读取 articles 表，不要求 AI 成功
      ↓ 每篇可链接到
    AI Research HTML（html）：只显示 AI 分析成功的文章

设计语言（对应"daily HTML 标准阅读器"，非 Dashboard）：

- 窄版居中阅读布局：body 浅灰/近白背景，中间约 720px 白色阅读区域；
- 顶部标题区：``ECO News — Daily Reader`` / 日期 / ``N articles · ECO – Economia Online``；
- Table of Contents（``#toc``）：N 篇新闻全部列出，点击标题跳转到对应文章；
- 每篇使用连续阅读的 ``<section id="article-N">``，不是卡片列表；
- 每篇包含：标题 / 发布时间 / 来源 / 作者 / AI 中文摘要（如已分析）/ 原文正文 / 原文链接 / Back to Contents；
- 正文用衬线字体 + 较宽行距，适合长时间阅读；
- AI 状态三态保留：✓ 已分析（显示摘要 + Research 入口）/ ⚠ 失败（不影响原文）/ ○ 尚未分析；
- 阅读器交互：已读 □/✓、收藏 ☆/★、阅读进度、localStorage 保存、Back to Contents、
  J/K 上下篇快捷键、阅读模式切换（Day/Sepia/Night）；
- 浏览器扩展兼容：标准 UTF-8、语义化 article/section/p/h1/h2、正文是普通 DOM 文本、
  不用 canvas/iframe/shadow DOM、不依赖外部 CDN、不用复杂 JS 框架；
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

from .beijing import fmt_date as _bj_fmt_date, fmt_dt as _bj_fmt_dt
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
    """把 ISO 时间字符串格式化为北京时间（Asia/Shanghai）YYYY-MM-DD HH:MM（24 小时制）；空值返回 '—'。"""
    return _bj_fmt_dt(value)


def _fmt_date(value: Any) -> str:
    """把 ISO 时间字符串格式化为北京时间 YYYY-MM-DD；空值返回 '—'。"""
    return _bj_fmt_date(value)


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


# ======================================================================
# Daily Reader 设计语言（CSS / JS）
# ======================================================================

_READER_CSS = """
:root {
  --page-bg: #f4f4f1;          /* body 浅灰/近白背景 */
  --reader-bg: #ffffff;        /* 中间白色阅读区域 */
  --text: #1c1c1a;
  --text-soft: #5c5c58;
  --text-muted: #8a8a84;
  --hairline: #e3e3dd;
  --accent: #6b4f2a;           /* 阅读器暖棕强调色 */
  --accent-soft: #f2ecdd;
  --ok: #2f6f44;
  --ok-soft: #e6f2ea;
  --failed: #a03c2e;
  --failed-soft: #f7e9e6;
  --none-soft: #ecece6;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--page-bg);
  color: var(--text);
  font-family: Georgia, "Times New Roman", "Songti SC", "SimSun", "Noto Serif CJK SC", serif;
  line-height: 1.85;
  font-size: 17px;
}
body.sepia {
  --page-bg: #efe8d5;
  --reader-bg: #f6efdc;
  --text: #43342b;
  --text-soft: #6b5645;
  --text-muted: #96816c;
  --hairline: #ddd3ba;
  --accent: #7a5b2e;
  --accent-soft: #e8ddc2;
}
body.night {
  --page-bg: #18181a;
  --reader-bg: #1f1f22;
  --text: #c9c9c4;
  --text-soft: #9b9b95;
  --text-muted: #777771;
  --hairline: #34343a;
  --accent: #c8a866;
  --accent-soft: #3a3527;
  --ok: #8fc49e;
  --ok-soft: #26382c;
  --failed: #e08a79;
  --failed-soft: #3a2723;
  --none-soft: #333338;
}

/* ---- 顶部固定工具条（进度 / 阅读模式） ---- */
.toolbar {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 50;
  height: 4px;
  background: transparent;
}
#progress-bar {
  height: 4px;
  width: 0%;
  background: var(--accent);
  transition: width .1s linear;
}
.reader-controls {
  position: fixed;
  top: 12px; right: 14px;
  z-index: 60;
  display: flex; gap: 8px; align-items: center;
  font-size: 12px;
}
.reader-controls button {
  font-family: inherit;
  font-size: 12px;
  padding: 4px 10px;
  border: 1px solid var(--hairline);
  border-radius: 999px;
  background: var(--reader-bg);
  color: var(--text-soft);
  cursor: pointer;
}
.reader-controls button:hover { color: var(--accent); border-color: var(--accent); }
.reader-controls .mode-label { color: var(--text-muted); }

/* ---- 阅读容器：窄版居中 ---- */
.reader {
  max-width: 720px;
  margin: 0 auto;
  padding: 56px 24px 80px;
}
.masthead {
  background: var(--reader-bg);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 30px 40px;
  margin-bottom: 28px;
}
.masthead h1 {
  font-size: 26px;
  font-weight: 700;
  margin: 0 0 6px;
  line-height: 1.3;
  letter-spacing: .01em;
}
.masthead .date {
  color: var(--text-soft);
  font-size: 14px;
  margin: 2px 0;
}
.masthead .count {
  color: var(--text-soft);
  font-size: 14px;
  margin: 2px 0;
}
.masthead .sub {
  color: var(--text-muted);
  font-size: 13px;
  margin: 10px 0 0;
  border-top: 1px solid var(--hairline);
  padding-top: 12px;
}
.masthead .sub .ai-badge { margin-right: 6px; }

/* ---- Table of Contents ---- */
.toc {
  background: var(--reader-bg);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 24px 40px;
  margin-bottom: 28px;
}
.toc h2 {
  font-size: 15px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--text-soft);
  margin: 0 0 14px;
  border-bottom: 1px solid var(--hairline);
  padding-bottom: 8px;
}
.toc ol {
  margin: 0;
  padding: 0;
  list-style: none;
  counter-reset: toc;
  columns: 1;
}
.toc li {
  counter-increment: toc;
  margin: 0 0 4px;
}
.toc li a {
  display: inline-block;
  color: var(--text);
  text-decoration: none;
  font-size: 14.5px;
  line-height: 1.6;
  border-bottom: 1px dotted transparent;
}
.toc li a::before {
  content: counter(toc) ".";
  color: var(--text-muted);
  margin-right: 8px;
  font-size: 13px;
}
.toc li a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.toc li a.starred::after { content: " ★"; color: var(--accent); font-size: 12px; }

/* ---- 文章 section：连续阅读 ---- */
section.article {
  background: var(--reader-bg);
  border: 1px solid var(--hairline);
  border-radius: 10px;
  padding: 28px 40px;
  margin-bottom: 24px;
  scroll-margin-top: 24px;
}
.article-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 6px;
}
.read-toggle, .star-toggle {
  flex: 0 0 auto;
  cursor: pointer;
  user-select: none;
  font-size: 16px;
  color: var(--text-muted);
  border: none;
  background: none;
  padding: 0;
  line-height: 1;
}
.read-toggle.read, .star-toggle.starred { color: var(--accent); }
h2.article-title {
  font-size: 21px;
  font-weight: 700;
  margin: 0;
  line-height: 1.4;
}
.article-meta {
  color: var(--text-muted);
  font-size: 13px;
  margin: 4px 0 12px;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
.ai-line {
  margin: 10px 0;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ai-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.ai-ok { background: var(--ok-soft); color: var(--ok); }
.ai-failed { background: var(--failed-soft); color: var(--failed); }
.ai-none { background: var(--none-soft); color: var(--text-soft); }

.ai-summary {
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  padding: 12px 16px;
  margin: 10px 0 14px;
  border-radius: 0 8px 8px 0;
}
.ai-summary .label {
  display: block;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .05em;
  color: var(--accent);
  margin-bottom: 6px;
  text-transform: uppercase;
}
.ai-summary .text {
  white-space: pre-wrap;
  font-size: 15.5px;
  line-height: 1.8;
  color: var(--text);
}
.ai-note {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 13.5px;
  color: var(--text-soft);
  margin: 10px 0;
}

/* 原文正文：衬线 + 宽行距 */
.original-body {
  white-space: pre-wrap;
  font-size: 16.5px;
  line-height: 1.9;
  color: var(--text);
  border-top: 1px dashed var(--hairline);
  margin-top: 12px;
  padding-top: 14px;
}
.original-body p { margin: 0 0 1em; }
.empty-body { color: var(--text-muted); font-style: italic; }

.article-links {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 13.5px;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.article-links a { color: var(--accent); text-decoration: none; }
.article-links a:hover { text-decoration: underline; }

.footer {
  text-align: center;
  color: var(--text-muted);
  font-size: 12px;
  margin-top: 40px;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

/* ---- 单篇独立页通用 ---- */
.page-title {
  font-size: 26px; font-weight: 700; margin: 0 0 8px; line-height: 1.35;
}
.breadcrumb { font-size: 13px; margin-bottom: 18px; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
.breadcrumb a { color: var(--accent); text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.meta-grid { font-size: 14px; color: var(--text-soft); margin: 10px 0; }
.meta-grid .k { color: var(--text-muted); }
.meta-grid .v { word-break: break-all; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.chip {
  display: inline-block;
  padding: 2px 12px;
  border-radius: 999px;
  font-size: 13px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
ol.key-points { padding-left: 24px; margin: 8px 0; }
ol.key-points li { margin-bottom: 6px; }
table.entities { border-collapse: collapse; width: 100%; margin: 8px 0; }
table.entities th, table.entities td {
  border: 1px solid var(--hairline);
  padding: 6px 12px;
  text-align: left;
  font-size: 14px;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
table.entities th { background: var(--none-soft); }
.mr-badge {
  display: inline-block;
  font-size: 13px;
  font-weight: 700;
  padding: 3px 12px;
  border-radius: 8px;
  letter-spacing: .05em;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.mr-high { background: var(--failed-soft); color: var(--failed); }
.mr-medium { background: #f4e9c8; color: #7d4e00; }
.mr-low { background: var(--ok-soft); color: var(--ok); }
.btn {
  display: inline-block;
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 13px;
  font-weight: 600;
  padding: 6px 16px;
  border-radius: 8px;
  text-decoration: none;
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { opacity: .9; }
.btn-outline { border: 1px solid var(--hairline); color: var(--accent); background: var(--reader-bg); }
.btn-outline:hover { background: var(--accent-soft); }
.section-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: .04em;
  color: var(--accent);
  margin: 22px 0 10px;
  border-bottom: 1px solid var(--hairline);
  padding-bottom: 6px;
}
@media (max-width: 640px) {
  .reader { padding: 48px 12px 60px; }
  .masthead, .toc, section.article { padding: 18px 18px; }
  h2.article-title { font-size: 18px; }
}
"""


_READER_JS = r"""
(function () {
  'use strict';
  var STORAGE_KEY = 'laxinwen.news.reader.v1';

  function loadState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    } catch (e) { return {}; }
  }
  function saveState(s) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch (e) {}
  }
  var state = loadState();
  if (!state.read) state.read = {};
  if (!state.star) state.star = {};

  // ---- 已读 / 收藏 ----
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    if (!t || !t.classList) return;

    if (t.classList.contains('read-toggle')) {
      var id = t.getAttribute('data-id');
      state.read[id] = !state.read[id];
      t.classList.toggle('read', state.read[id]);
      t.textContent = state.read[id] ? '✓' : '□';
      t.title = state.read[id] ? '标记为未读' : '标记为已读';
      saveState(state);
      return;
    }
    if (t.classList.contains('star-toggle')) {
      var sid = t.getAttribute('data-id');
      state.star[sid] = !state.star[sid];
      t.classList.toggle('starred', state.star[sid]);
      t.textContent = state.star[sid] ? '★' : '☆';
      t.title = state.star[sid] ? '取消收藏' : '收藏';
      saveState(state);
      // 同步目录收藏标记
      var tocLink = document.querySelector('.toc a[href="#article-' + sid + '"]');
      if (tocLink) tocLink.classList.toggle('starred', state.star[sid]);
      return;
    }
  });

  // ---- 初始化已读/收藏状态 ----
  document.querySelectorAll('.read-toggle').forEach(function (el) {
    var id = el.getAttribute('data-id');
    if (state.read[id]) { el.classList.add('read'); el.textContent = '✓'; el.title = '标记为未读'; }
  });
  document.querySelectorAll('.star-toggle').forEach(function (el) {
    var id = el.getAttribute('data-id');
    if (state.star[id]) { el.classList.add('starred'); el.textContent = '★'; el.title = '取消收藏'; }
  });

  // ---- 阅读进度 ----
  var bar = document.getElementById('progress-bar');
  function updateProgress() {
    var doc = document.documentElement;
    var total = doc.scrollHeight - doc.clientHeight;
    var pct = total > 0 ? (doc.scrollTop / total) * 100 : 0;
    if (bar) bar.style.width = pct.toFixed(2) + '%';
  }
  window.addEventListener('scroll', updateProgress, { passive: true });
  window.addEventListener('resize', updateProgress);
  updateProgress();

  // ---- 阅读模式切换（Day / Sepia / Night）----
  var MODES = ['day', 'sepia', 'night'];
  var modeBtn = document.getElementById('mode-toggle');
  var modeLabel = document.getElementById('mode-label');
  var mode = state.mode || 'day';
  function applyMode(m) {
    mode = m;
    state.mode = m;
    saveState(state);
    document.body.classList.remove('sepia', 'night');
    if (m !== 'day') document.body.classList.add(m);
    if (modeLabel) modeLabel.textContent = m.charAt(0).toUpperCase() + m.slice(1);
  }
  applyMode(mode);
  if (modeBtn) {
    modeBtn.addEventListener('click', function () {
      var next = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      applyMode(next);
    });
  }

  // ---- J/K 上下篇快捷键 ----
  function visibleSections() {
    return Array.prototype.slice.call(document.querySelectorAll('section.article'));
  }
  function currentTop() {
    return window.pageYOffset || document.documentElement.scrollTop || 0;
  }
  document.addEventListener('keydown', function (ev) {
    var tag = (ev.target && ev.target.tagName) || '';
    if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
    var sections = visibleSections();
    if (!sections.length) return;
    var key = ev.key;
    if (key === 'j' || key === 'J') {      // 下一篇
      var next = null;
      for (var i = 0; i < sections.length; i++) {
        var top = sections[i].getBoundingClientRect().top;
        if (top > 6) { next = sections[i]; break; }
      }
      if (next) next.scrollIntoView();
    } else if (key === 'k' || key === 'K') { // 上一篇
      var prev = null;
      for (var j = 0; j < sections.length; j++) {
        var t2 = sections[j].getBoundingClientRect().top;
        if (t2 > 6) break;
        prev = sections[j];
      }
      if (prev) prev.scrollIntoView();
    }
  });

  // ---- 恢复阅读位置（可选：记住上次滚动位置）----
  if (state.scrollY && history.scrollRestoration !== 'manual') {
    try {
      window.scrollTo(0, state.scrollY);
    } catch (e) {}
  }
  window.addEventListener('scroll', function () {
    state.scrollY = currentTop();
  }, { passive: true });
})();
"""


# ======================================================================
# 文章 section 渲染（连续阅读）
# ======================================================================

def _render_ai_summary_box(analysis: dict[str, Any], research_rel: str = "") -> str:
    """已分析文章的 AI 摘要区块 + Research 入口。"""
    summary = analysis.get("summary_zh") or ""
    parts: list[str] = []
    if summary:
        parts.append(
            f'<div class="ai-summary"><span class="label">AI 中文摘要</span>'
            f'<div class="text">{_e(summary)}</div></div>'
        )
    else:
        parts.append('<p class="ai-note">（该文章已完成 AI 分析，但暂无中文摘要文本）</p>')
    if research_rel:
        parts.append(
            f'<p class="article-links" style="margin-top:0;border-top:none;padding-top:0;">'
            f'<a class="btn btn-primary" href="{_e(research_rel)}" rel="noopener" target="_blank">'
            f'查看 AI Research →</a></p>'
        )
    return "\n".join(parts)


def _render_ai_note(status: str) -> str:
    """未成功分析文章的说明（不影响原文阅读）。"""
    if status == "failed":
        return (
            '<p class="ai-note"><span class="ai-badge ai-failed">⚠ AI 分析失败</span> '
            '该文章之前尝试进行 AI 分析但失败了（不影响原文阅读）。</p>'
        )
    return (
        '<p class="ai-note"><span class="ai-badge ai-none">○ 尚未分析</span> '
        '该文章尚未进行 AI 分析。运行 <code>news process</code> 可为其生成中文摘要与观点。</p>'
    )


def render_article_section(
    *,
    index: int,
    article_id: int,
    title: str,
    source_name: str,
    authors: list[str],
    published_at: Any,
    canonical_url: str,
    body_text: str = "",
    body_html: Optional[str] = None,
    ai_status: str,
    analysis: dict[str, Any],
    research_rel: str = "",
) -> str:
    """渲染一篇新闻为连续阅读的 ``<section id="article-N">``。

    正文优先级：
    - ``body_html`` 存在 → 直接作为 HTML 渲染（保留段落/图片等格式）；
    - ``body_html`` 不存在 → 回退到 ``body_text``（HTML escape 后渲染）。
    ECO/HKEJ 只存 ``body_text`` 时行为不变。
    """
    n = index  # article-N 的 N 为目录序号（1 起），保证锚点可读
    dt = _fmt_dt(published_at)
    author_str = ", ".join(authors) if authors else ""

    head = (
        f'<div class="article-head">'
        f'<button type="button" class="read-toggle" data-id="{n}" title="标记为已读" aria-label="标记已读">□</button>'
        f'<button type="button" class="star-toggle" data-id="{n}" title="收藏" aria-label="收藏">☆</button>'
        f'<h2 class="article-title">{_e(title)}</h2>'
        f'</div>'
    )
    meta_parts = [f'<span>{_e(dt)}</span>', f'<span>{_e(source_name)}</span>']
    if author_str:
        meta_parts.append(f'<span>作者：{_e(author_str)}</span>')
    meta = f'<p class="article-meta">{" · ".join(meta_parts)}</p>'

    # AI 区块
    if ai_status == "ok":
        ai_block = (
            f'<div class="ai-line">{_ai_status_html("ok")}</div>'
            + _render_ai_summary_box(analysis, research_rel=research_rel)
        )
    else:
        ai_block = _render_ai_note(ai_status)

    # 原文正文：body_html 存在则直接 HTML 渲染；否则 fallback 到 body_text
    if body_html and body_html.strip():
        body_render = f'<div class="original-body">{body_html}</div>'
    elif body_text and body_text.strip():
        body_render = f'<div class="original-body">{_e(body_text)}</div>'
    else:
        body_render = '<p class="empty-body">（暂无原文正文）</p>'

    # 链接
    links: list[str] = [
        '<a href="#toc">↑ Back to Contents</a>',
    ]
    if canonical_url:
        links.append(
            f'<a href="{_e(canonical_url)}" rel="noopener" target="_blank">阅读原文 →</a>'
        )
    links_html = '<div class="article-links">' + "".join(links) + "</div>"

    return f"""<section class="article" id="article-{n}" data-id="{n}">
  {head}
  {meta}
  {ai_block}
  {body_render}
  {links_html}
</section>"""


# ======================================================================
# index.html —— Daily Reader 总页
# ======================================================================

def _toc_entries(rows: list) -> list[dict]:
    """构造目录条目（序号 / 标题 / 文章 id）。"""
    out: list[dict] = []
    for i, row in enumerate(rows, start=1):
        r = dict(row)
        out.append(
            {
                "n": i,
                "article_id": int(r["id"]),
                "title": r["title"] or "",
            }
        )
    return out


def _render_toc_html(entries: list[dict]) -> str:
    items: list[str] = []
    for e in entries:
        items.append(
            f'<li><a href="#article-{e["n"]}">{_e(e["title"])}</a></li>'
        )
    return (
        '<nav class="toc" id="toc" aria-label="Table of Contents">'
        f'<h2>Table of Contents</h2><ol>{"".join(items)}</ol></nav>'
    )


def render_reader_index_html(
    rows: list,
    *,
    source_name: str,
    total: int,
    analyzed_ok: int,
    analyzed_failed: int,
    unanalyzed: int,
    research_rel_by_article: dict[int, str],
    generated_at: datetime,
    analysis_by_article: dict[int, dict[str, Any]],
) -> str:
    """渲染 Daily Reader 首页：标题区 + Table of Contents + 全部新闻 section。

    100 篇新闻全部完整列出（不截断成摘要卡片），每篇可连续阅读。
    """
    toc_entries = _toc_entries(rows)

    sections: list[str] = []
    for i, row in enumerate(rows, start=1):
        r = dict(row)
        article_id = int(r["id"])
        try:
            authors = json.loads(r["authors"] or "[]")
        except (ValueError, TypeError):
            authors = []
        status = _ai_status(r)
        sections.append(
            render_article_section(
                index=i,
                article_id=article_id,
                title=r["title"] or "",
                source_name=r["source_name"] or r["source_id"] or source_name,
                authors=authors,
                published_at=(
                    _parse_datetime(r["published_at"])
                    or _parse_datetime(r["discovered_at"])
                ),
                canonical_url=r["canonical_url"] or "",
                body_text=r["body_text"] or "",
                body_html=r.get("body_html") or "",
                ai_status=status,
                analysis=analysis_by_article.get(article_id, {}),
                research_rel=research_rel_by_article.get(article_id, ""),
            )
        )
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
<title>{_e(source_name)} News — Daily Reader · laxinwen</title>
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
    <h1>{_e(source_name)} News — Daily Reader</h1>
    <p class="date">{_bj_fmt_date(generated_at)}</p>
    <p class="count">{total} articles · {_e(source_name)}</p>
    <p class="sub">
      最近 {total} 条 · 按发布日期倒序 · 更新时间 {_bj_fmt_dt(generated_at)}（北京时间）
      <br>{stats_line}
    </p>
  </header>

  {_render_toc_html(toc_entries)}

  <main>
    {sections_html}
  </main>

  <div class="footer">
    由 laxinwen 生成 · News Archive Daily Reader（最近 {total} 条新闻阅读目录） ·
    快捷键 J/K 切换上下篇 · □/☆ 已读/收藏（localStorage 保存）
  </div>
</div>

<script>{_READER_JS}</script>
</body>
</html>
"""


# ======================================================================
# 单篇独立页（render_article_page，保留给 CLI/旧目录复用）
# ======================================================================

def render_article_page(
    *,
    article_id: int,
    title: str,
    source_name: str,
    authors: list[str],
    published_at: Any,
    canonical_url: str,
    body_text: str = "",
    body_html: Optional[str] = None,
    ai_status: str,
    analysis: dict[str, Any],
    index_rel: str = "index.html",
    research_rel: str = "",
) -> str:
    """渲染单篇 News Archive 页面（Daily Reader 风格，独立可读）。

    有 AI 成功分析 → 显示 AI 详情（摘要/关键观点/主题/实体/市场相关性/理由/语言）；
    无成功分析 → 显示"尚未进行 AI 分析"或"AI 分析失败"，随后显示原文。
    """
    has_ai = ai_status == "ok"

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
        kp_html = (
            f'<ol class="key-points">{kp_html}</ol>' if kp_html else '<p class="ai-note">（无）</p>'
        )
        chips = "".join(f'<span class="chip">{_e(t)}</span>' for t in topics)
        topics_html = f'<div class="chips">{chips}</div>' if chips else '<p class="ai-note">（无）</p>'

        rows_html = ""
        for ent in entities:
            if isinstance(ent, dict):
                rows_html += (
                    f"<tr><td>{_e(ent.get('name'))}</td><td>{_e(ent.get('type'))}</td></tr>"
                )
        entities_html = (
            '<table class="entities"><thead><tr><th>实体</th><th>类型</th></tr></thead>'
            f"<tbody>{rows_html}</tbody></table>"
            if rows_html
            else '<p class="ai-note">（无）</p>'
        )

        mr_cls = {"high": "mr-high", "medium": "mr-medium", "low": "mr-low"}.get(
            relevance, "mr-medium"
        )
        mr_label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(
            relevance, (relevance or "—").upper()
        )
        reason_html = (
            f'<p class="ai-note"><strong>分析理由：</strong> {_e(reason)}</p>' if reason else ""
        )

        ai_section = f"""
  <h2 class="section-title">AI 分析</h2>
  <p>{_ai_status_html('ok')}</p>
  <h3 style="font-size:15px;margin:14px 0 6px;">中文摘要</h3>
  <div class="ai-summary"><span class="label">AI 中文摘要</span><div class="text">{_e(summary)}</div></div>
  <h3 style="font-size:15px;margin:14px 0 6px;">关键观点</h3>
  {kp_html}
  <h3 style="font-size:15px;margin:14px 0 6px;">主题</h3>
  {topics_html}
  <h3 style="font-size:15px;margin:14px 0 6px;">实体</h3>
  {entities_html}
  <h3 style="font-size:15px;margin:14px 0 6px;">市场相关性</h3>
  <p><span class="mr-badge {mr_cls}">{_e(mr_label)}</span> <span class="ai-note">（AI 判断，不代表原文事实）</span></p>
  {reason_html}
  <h3 style="font-size:15px;margin:14px 0 6px;">原文语言</h3>
  <p class="ai-note">{_e(detected_lang) if detected_lang else '—'}</p>
"""
    else:
        ai_section = f"""
  <h2 class="section-title">AI 分析</h2>
  {_render_ai_note(ai_status)}
"""

    body_render = ""
    if body_html and body_html.strip():
        body_render = f'<div class="original-body">{body_html}</div>'
    elif body_text and body_text.strip():
        body_render = f'<div class="original-body">{_e(body_text)}</div>'
    else:
        body_render = '<p class="empty-body">（暂无原文正文）</p>'

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
        f'<p class="article-links" style="border-top:none;padding-top:0;">{research_link} {original_link}</p>'
        if (research_link or original_link)
        else ""
    )

    author_str = ", ".join(authors) if authors else "—"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · News Archive · laxinwen</title>
<style>{_READER_CSS}</style>
</head>
<body>

<div class="toolbar"><div id="progress-bar"></div></div>
<div class="reader-controls">
  <span class="mode-label" id="mode-label">Day</span>
  <button type="button" id="mode-toggle" title="切换阅读模式（Day / Sepia / Night）">阅读模式</button>
</div>

<div class="reader">
  <div class="breadcrumb"><a href="{_e(index_rel)}">← 返回新闻列表</a></div>

  <header class="masthead">
    <div class="article-head" style="margin-bottom:4px;">
      <button type="button" class="read-toggle" data-id="{article_id}" title="标记为已读" aria-label="标记已读">□</button>
      <button type="button" class="star-toggle" data-id="{article_id}" title="收藏" aria-label="收藏">☆</button>
      <h1 class="page-title">{_e(title)}</h1>
    </div>
    <p class="article-meta">
      <span>{_e(_fmt_dt(published_at))}</span>
      <span>{_e(source_name)}</span>
      <span>作者：{_e(author_str)}</span>
    </p>
    <div class="meta-grid">
      <div><span class="k">来源：</span><span class="v">{_e(source_name)}</span></div>
      <div><span class="k">原文链接：</span><span class="v"><a href="{_e(canonical_url)}" rel="noopener" target="_blank">{_e(canonical_url)}</a></span></div>
      <div><span class="k">AI 状态：</span><span class="v">{_ai_status_html(ai_status)}</span></div>
    </div>
    {action_bar}
  </header>

  {ai_section}

  <h2 class="section-title">原文正文</h2>
  {body_render}

  <div class="footer">
    由 laxinwen 生成 · News Archive（最近 N 条新闻阅读目录）·
    <a href="#toc" style="color:inherit;">↑</a>
  </div>
</div>

<script>{_READER_JS}</script>
</body>
</html>
"""


# ======================================================================
# 顶层导出
# ======================================================================

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
        ├── index.html            # Daily Reader 总页（目录 + 全部文章 section）
        └── YYYY/MM/0001-<slug>.html   # 单篇独立页（保留，便于直接分享/打印）

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
    # 只导出 usable article（排除 failed / 空标题 / 空正文 / [抓取失败] 前缀）
    rows = [r for r in rows if storage.is_usable(r)]
    result = NewsArchiveResult()

    # article_id -> AI Research 相对路径（若存在成功分析，指向 data/export/html/ 对应页面）
    research_rel_by_article: dict[int, str] = {}

    # 查找 AI Research HTML 导出目录（默认 data/export/html/）
    research_root = _locate_research_root(out_dir)
    if research_root is not None:
        import os as _os

        rel_prefix = _os.path.relpath(str(research_root), start=str(out_dir)).replace("\\", "/")
        for rrow in storage.list_analysis_success(source_id=source_id, limit=10**9):
            art_id = int(rrow["article_id"])
            pub = _parse_datetime(rrow["published_at"]) or _parse_datetime(rrow["discovered_at"])
            fname = _build_filename(art_id, rrow["art_title"] or "", pub)
            month = f"{pub:%Y}/{pub:%m}" if pub else "unknown/00"
            research_rel_by_article[art_id] = f"{rel_prefix}/{month}/{fname}"

    # 预取成功分析（供 index 中已分析文章的摘要/详情）
    analysis_by_article: dict[int, dict[str, Any]] = {}
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

    # 单篇独立页（保留现有行为，供旧目录/分享/打印）
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

    # index.html —— Daily Reader 总页
    result.index_path = root / "index.html"
    index_html = render_reader_index_html(
        rows,
        source_name=rows[0]["source_name"] if rows else source_id,
        total=len(rows),
        analyzed_ok=result.analyzed_ok,
        analyzed_failed=result.analyzed_failed,
        unanalyzed=result.unanalyzed,
        research_rel_by_article=research_rel_by_article,
        generated_at=datetime.now(timezone.utc),
        analysis_by_article=analysis_by_article,
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
        body_html=row["body_html"] or "",
        ai_status=status,
        analysis=analysis,
        index_rel="../../index.html",
        research_rel=research_rel,
    )
    month_dir.joinpath(filename).write_text(html_doc, encoding="utf-8")
    return Path(f"{month_dir.relative_to(root).as_posix()}/{filename}")
