"""ECO News Reader —— 轻量级 Windows 桌面 GUI（tkinter / ttk）。

设计目标：
- 只是现有 CLI / pipeline 的**用户界面层**，不重新实现任何抓取逻辑；
- 抓取 / AI 分析都调用现有 ``Pipeline`` / ``ArticleProcessor`` / ``export_*``；
- 全程异步执行，网络请求不阻塞 GUI 主线程；
- 错误不崩溃：任何阶段失败都在日志区显示，并恢复按钮状态；
- 第一版零新依赖：Python 标准库 ``tkinter / ttk / threading / queue``。

启动方式：
    uv run news gui
    双击 NewsReader.bat（Windows）

命令行选项：
    news gui [--db PATH] [--site eco]
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import messagebox, ttk


# ---------- 路径 / 默认值 ----------

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DB = Path(
    os.environ.get(
        "NEWS_DB",
        str(_PROJECT_ROOT / "data" / "news.db"),
    )
)

# News Archive（阅读目录）与 AI Research（研究结果）导出目录
DEFAULT_NEWS_ARCHIVE_DIR = _PROJECT_ROOT / "data" / "export" / "news-html"
DEFAULT_RESEARCH_DIR = _PROJECT_ROOT / "data" / "export" / "html"

_QUICK_LIMITS = (50, 100, 200)
_DEFAULT_LIMIT = 100
_DEFAULT_AI_LIMIT = 3

_APP_TITLE = "ECO News Reader"

# 后台线程完成哨兵 → 恢复提示文案
_DONE_SENTINELS = {
    "__FETCH_DONE__": "抓取",
    "__AI_DONE__": "AI 分析",
    "__ARCHIVE_DONE__": "打开新闻库",
    "__RESEARCH_DONE__": "打开 AI 研究结果",
}


class _NewsReaderApp:
    """tkinter 主窗口。业务逻辑全部通过回调注入，便于离线测试。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        db_path: str | Path = DEFAULT_DB,
        site: str = "eco",
        site_name: str = "ECO",
        storage_factory=None,
        pipeline_factory=None,
        processor_factory=None,
        news_archive_export=None,
        research_export=None,
        open_url=None,
        news_archive_dir: str | Path = DEFAULT_NEWS_ARCHIVE_DIR,
        research_dir: str | Path = DEFAULT_RESEARCH_DIR,
    ):
        self.root = root
        self.db_path = Path(db_path)
        self.site = site
        self.site_name = site_name
        self.news_archive_dir = Path(news_archive_dir)
        self.research_dir = Path(research_dir)

        # 依赖注入（默认走真实实现；测试可替换为假实现）
        self._storage_factory = storage_factory or _default_storage_factory
        self._pipeline_factory = pipeline_factory or _default_pipeline_factory
        self._processor_factory = processor_factory or _default_processor_factory
        self._news_archive_export = news_archive_export or _default_news_archive_export
        self._research_export = research_export or _default_research_export
        self._open_url = open_url or _default_open_url

        # 后台线程 → GUI 消息队列
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._busy = False

        # 初始化时先建好数据库（含 schema），后续线程各自打开独立连接
        with self._storage_factory(self.db_path) as storage:
            self._last_fetch_at = self._read_last_fetch_at(storage)
            self._analysis_status = self._read_analysis_status(storage)

        self._build_ui()
        self._refresh_status()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.root.title(_APP_TITLE)
        self.root.geometry("760x640")
        self.root.minsize(620, 520)
        self.root.option_add("*tearOff", False)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", font=("", 10, "bold"))
        style.configure("TLabel", font=("", 10))
        style.configure("TLabelframe.Label", font=("", 10, "bold"))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- 抓取设置卡片 ----
        card = ttk.LabelFrame(outer, text="抓取设置", padding=10)
        card.pack(fill="x")

        row1 = ttk.Frame(card)
        row1.pack(fill="x")
        ttk.Label(row1, text="新闻网站：").pack(side="left")
        self.site_var = tk.StringVar(value=self.site)
        self.site_combo = ttk.Combobox(
            row1, textvariable=self.site_var, state="readonly", width=14
        )
        self.site_combo["values"] = [self.site]
        self.site_combo.pack(side="left", padx=(4, 20))

        ttk.Label(row1, text="抓取数量：").pack(side="left")
        self.limit_var = tk.StringVar(value=str(_DEFAULT_LIMIT))
        self.limit_entry = ttk.Entry(row1, textvariable=self.limit_var, width=8)
        self.limit_entry.pack(side="left", padx=4)

        quick = ttk.Frame(row1)
        quick.pack(side="left", padx=(6, 0))
        for n in _QUICK_LIMITS:
            ttk.Button(quick, text=str(n), width=5, command=lambda v=n: self._set_limit(v)).pack(
                side="left", padx=2
            )

        row2 = ttk.Frame(card)
        row2.pack(fill="x", pady=(10, 0))

        self.fetch_btn = ttk.Button(
            row2, text="抓取最新新闻", style="Accent.TButton", command=self._on_fetch
        )
        self.fetch_btn.pack(side="left")

        self.export_btn = ttk.Button(
            row2, text="📖 打开新闻库", command=self._on_open_news_archive
        )
        self.export_btn.pack(side="left", padx=(10, 0))

        ttk.Label(row2, text="AI 分析数量：").pack(side="left", padx=(24, 4))
        self.ai_limit_var = tk.StringVar(value=str(_DEFAULT_AI_LIMIT))
        self.ai_limit_entry = ttk.Entry(row2, textvariable=self.ai_limit_var, width=5)
        self.ai_limit_entry.pack(side="left")

        self.ai_btn = ttk.Button(
            row2, text="🤖 AI 分析", command=self._on_ai_analyze
        )
        self.ai_btn.pack(side="left", padx=(6, 0))

        self.research_btn = ttk.Button(
            row2, text="📊 打开 AI 研究结果", command=self._on_open_research
        )
        self.research_btn.pack(side="left", padx=(10, 0))

        # ---- 状态卡片 ----
        self.status_card = ttk.LabelFrame(outer, text="状态", padding=10)
        self.status_card.pack(fill="x", pady=(10, 0))
        self.status_labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(self.status_card)
        grid.pack(fill="x")
        for i, key in enumerate(
            ("db", "eco_count", "ai_ok", "ai_failed", "last_fetch")
        ):
            lbl = ttk.Label(grid, text="")
            lbl.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 24), pady=1)
            self.status_labels[key] = lbl

        # ---- 日志区 ----
        log_card = ttk.LabelFrame(outer, text="日志", padding=8)
        log_card.pack(fill="both", expand=True, pady=(10, 0))

        self.log_text = tk.Text(
            log_card,
            height=10,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_card, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

        self._sep = "─" * 58
        self.log(
            f"ECO News Reader 已就绪 · 数据库：{self.db_path}\n"
            f"发现→去重→下载→提取→入库 使用现有 pipeline，"
            "不会绕过去重逻辑。"
        )

    # ------------------------------------------------------------------ 工具

    def _set_limit(self, value: int) -> None:
        self.limit_var.set(str(value))

    def _parse_limit(self, raw: Optional[str], *, what: str) -> Optional[int]:
        """把字符串解析为正整数；非法返回 None 并在日志提示。"""
        text = (raw or "").strip()
        if not text:
            self.log(f"请输入{what}数量（正整数）。")
            return None
        try:
            n = int(text)
        except ValueError:
            self.log(f"无效的{what}数量：{text!r}（需要正整数）。")
            return None
        if n <= 0:
            self.log(f"无效的{what}数量：{text!r}（需要正整数）。")
            return None
        return n

    def log(self, msg: str) -> None:
        """GUI 主线程内直接写日志框。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        self._append_bg_log(f"[{stamp}] {msg}")

    def _append_bg_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self) -> None:
        """主线程轮询后台日志队列，避免跨线程操作 Tk 控件。"""
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg in _DONE_SENTINELS:
                    self._set_busy(False, run=_DONE_SENTINELS[msg])
                    self._refresh_status()
                else:
                    self.log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_busy(self, busy: bool, *, run: str) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.fetch_btn.configure(state=state)
        self.export_btn.configure(state=state)
        self.ai_btn.configure(state=state)
        self.research_btn.configure(state=state)
        self.site_combo.configure(state="disabled" if busy else "readonly")
        for e in (self.limit_entry, self.ai_limit_entry):
            e.configure(state=state)
        if busy:
            self.log(f"⏳ {run} 进行中，请稍候……")
        else:
            self.log(f"✅ {run} 结束，按钮已恢复。")

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askokcancel(
                "退出", "后台任务仍在运行，确定要退出吗？"
            ):
                return
        self.root.destroy()

    def _status_text(self, value: str) -> str:
        try:
            if value and value.strip():
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value
        return "—"

    def _read_last_fetch_at(self, storage) -> Optional[str]:
        try:
            arts = storage.list_articles(source_id=self.site, limit=1)
            if not arts:
                return None
            fetched = arts[0].fetched_at or arts[0].discovered_at
            return fetched.isoformat() if fetched else None
        except Exception as exc:  # 只读统计，失败不影响界面
            logging.getLogger("news.gui").warning("读取最后抓取时间失败: %s", exc)
            return None

    def _read_analysis_status(self, storage) -> tuple[int, int]:
        try:
            ok = storage.count_analysis(source_id=self.site, status="success")
            failed = storage.count_analysis(source_id=self.site, status="failed")
            return ok, failed
        except Exception as exc:
            logging.getLogger("news.gui").warning("读取 AI 状态失败: %s", exc)
            return 0, 0

    # ------------------------------------------------------------------ 状态

    def _refresh_status(self) -> None:
        try:
            with self._storage_factory(self.db_path) as storage:
                eco_count = storage.count(source_id=self.site)
                self._last_fetch_at = self._read_last_fetch_at(storage)
                self._analysis_status = self._read_analysis_status(storage)
        except Exception as exc:
            self.log(f"状态读取失败：{exc}")
            eco_count = 0

        ai_ok, ai_failed = self._analysis_status
        values = {
            "db": f"数据库：{self.db_path}",
            "eco_count": f"{self.site_name} 新闻：{eco_count}",
            "ai_ok": f"AI 已分析：{ai_ok}",
            "ai_failed": f"AI 失败：{ai_failed}",
            "last_fetch": f"最后抓取：{self._status_text(self._last_fetch_at)}",
        }
        for key, text in values.items():
            self.status_labels[key].configure(text=text)

    # ------------------------------------------------------------------ 动作

    def _on_fetch(self) -> None:
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        limit = self._parse_limit(self.limit_var.get(), what="抓取")
        if limit is None:
            return
        self._set_busy(True, run="抓取")

        def worker() -> None:
            try:
                self._run_fetch(limit)
            except Exception as exc:
                self._bg_log(f"抓取失败：\n{exc}")
            finally:
                self._queue.put("__FETCH_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _on_ai_analyze(self) -> None:
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        limit = self._parse_limit(self.ai_limit_var.get(), what="AI 分析")
        if limit is None:
            return
        self._set_busy(True, run="AI 分析")

        def worker() -> None:
            try:
                self._run_ai_analyze(limit)
            except Exception as exc:
                self._bg_log(f"AI 分析失败：\n{exc}")
            finally:
                self._queue.put("__AI_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_news_archive(self) -> None:
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        limit = self._parse_limit(self.limit_var.get(), what="抓取")
        if limit is None:
            return
        self._set_busy(True, run="打开新闻库")

        def worker() -> None:
            try:
                self._run_news_archive(limit)
            except Exception as exc:
                self._bg_log(f"新闻库导出/打开失败：\n{exc}")
            finally:
                self._queue.put("__ARCHIVE_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_research(self) -> None:
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        self._set_busy(True, run="打开 AI 研究结果")

        def worker() -> None:
            try:
                self._run_research()
            except Exception as exc:
                self._bg_log(f"AI 研究结果导出/打开失败：\n{exc}")
            finally:
                self._queue.put("__RESEARCH_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _bg_log(self, msg: str) -> None:
        """后台线程安全地追加日志。"""
        self._queue.put(msg)

    # ------------------------------------------------------------------ 业务

    def _run_fetch(self, limit: int) -> None:
        self._bg_log(f"开始抓取 {self.site_name} 最新 {limit} 篇")
        # 打开新连接（线程内使用）
        with self._storage_factory(self.db_path) as storage:
            pipeline = self._pipeline_factory(storage, limit)
            try:
                stats = pipeline.run_site(self.site)
            finally:
                try:
                    pipeline.close()
                except Exception:
                    pass
        s = stats
        self._bg_log(
            f"{self._sep}\n"
            f"发现：{s.discovered}\n"
            f"重复：{s.skipped_dup}\n"
            f"新增：{s.fetched_ok}\n"
            f"失败：{s.failed}"
        )
        if s.errors:
            self._bg_log("失败明细（前 5 条）：")
            for err in s.errors[:5]:
                self._bg_log(f"  - {err}")
        self._bg_log(f"抓取完成（{self.site_name}，limit={limit}）")

    def _run_ai_analyze(self, limit: int) -> None:
        self._bg_log(f"开始 AI 分析（最多 {limit} 篇，复用现有 AI processing 逻辑）")
        with self._storage_factory(self.db_path) as storage:
            processor = self._processor_factory(storage)
            try:
                stats = processor.process_batch(source_id=self.site, limit=limit)
            finally:
                try:
                    processor.close()
                except Exception:
                    pass
        self._bg_log(
            f"{self._sep}\n"
            f"AI 处理：共 {stats.total} 篇 | 成功 {stats.ok} | 失败 {stats.failed}"
        )
        if stats.errors:
            self._bg_log("AI 失败明细（前 5 条）：")
            for err in stats.errors[:5]:
                self._bg_log(f"  - {err}")
        self._bg_log("AI 分析完成")

    def _run_news_archive(self, limit: int) -> None:
        out_dir = self.news_archive_dir / self.site
        self._bg_log(f"正在导出 News Archive（最近 {limit} 篇）→ {out_dir}")
        with self._storage_factory(self.db_path) as storage:
            result = self._news_archive_export(
                storage, out_dir, source_id=self.site, limit=limit
            )
        index = result.index_path or out_dir / "index.html"
        if not index.exists():
            raise FileNotFoundError(f"News Archive 未生成：{index}")
        self._bg_log(
            f"News Archive 导出完成：{result.exported} 篇（已分析 {result.analyzed_ok} / "
            f"失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）"
        )
        self._open_url(index.as_uri())
        self._bg_log(f"已在默认浏览器打开：{index}")

    def _run_research(self) -> None:
        out_dir = self.research_dir
        self._bg_log(f"正在导出 AI 研究结果 HTML → {out_dir}")
        with self._storage_factory(self.db_path) as storage:
            result = self._research_export(
                storage, out_dir, source_id=self.site
            )
        index = result.index_path or out_dir / "index.html"
        if not index.exists():
            raise FileNotFoundError(f"AI 研究结果未生成：{index}")
        self._bg_log(
            f"AI 研究结果导出完成：成功 {result.analysis_ok} / 失败 {result.analysis_failed}"
        )
        self._open_url(index.as_uri())
        self._bg_log(f"已在默认浏览器打开：{index}")


# ---------- 默认实现（真实逻辑；测试可注入假实现） ----------


def _default_storage_factory(db_path: str | Path):
    from .storage import Storage

    return Storage(db_path)


def _default_pipeline_factory(storage, limit: int):
    from .fetch import FetcherOptions, HttpxFetcher
    from .pipeline import Pipeline

    fetcher = HttpxFetcher(
        FetcherOptions(timeout=20.0, retries=3, min_interval=2.0, max_interval=4.0)
    )
    return Pipeline(storage, fetcher=fetcher, max_items=limit)


def _default_processor_factory(storage):
    from .ai import ArticleProcessor, load_dotenv

    load_dotenv()  # 支持项目根 .env（不覆盖已有环境变量）
    return ArticleProcessor(storage)


def _default_news_archive_export(storage, out_dir, *, source_id, limit):
    from .news_archive import export_news_archive

    return export_news_archive(storage, out_dir, source_id=source_id, limit=limit)


def _default_research_export(storage, out_dir, *, source_id):
    from .html_export import export_html

    return export_html(storage, out_dir, source_id=source_id)


def _default_open_url(url: str) -> None:
    webbrowser.open(url)


# ---------- 启动入口 ----------


def run_gui(
    *,
    db_path: str | Path = DEFAULT_DB,
    site: str = "eco",
) -> int:
    """启动 tkinter 主循环（供 ``news gui`` 与 NewsReader.bat 调用）。"""
    root = tk.Tk()
    app = _NewsReaderApp(
        root,
        db_path=db_path,
        site=site,
        site_name=_site_display_name(site),
    )
    root.mainloop()
    return 0


def _site_display_name(site: str) -> str:
    try:
        from .config import load_site_config

        cfg = load_site_config(site)
        return str(cfg.get("name") or site)
    except Exception:
        return site.upper()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``news gui``。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="news gui",
        description="ECO News Reader —— 轻量级 Windows 桌面 GUI",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--site", default="eco", help="站点 id（当前仅 ECO）")
    args = parser.parse_args(argv)
    return run_gui(db_path=args.db, site=args.site)


if __name__ == "__main__":
    sys.exit(main())
