"""Laxinwen News Reader —— 轻量级 Windows 桌面 GUI（tkinter / ttk）。

设计目标：
- 只是现有 CLI / pipeline 的**用户界面层**，不重新实现任何抓取逻辑；
- 新闻来源可在 ECO / HKEJ / 全部 之间切换，后台统一复用现有 pipeline；
- 抓取 / AI 分析都调用现有 ``Pipeline`` / ``ArticleProcessor`` / ``export_*``；
- 全程异步执行，网络请求不阻塞 GUI 主线程；
- 错误不崩溃：任何阶段失败都在日志区显示，并恢复按钮状态；
- 零新依赖：Python 标准库 ``tkinter / ttk / threading / queue``。

启动方式：
    uv run news gui
    双击 NewsReader.bat（Windows）

命令行选项：
    news gui [--db PATH] [--site eco|hkej]
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

from .reader_server import ReaderServer


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
# 便携式导出目录（独立 HTML / HTML 新闻包）
DEFAULT_PORTABLE_DIR = _PROJECT_ROOT / "data" / "export" / "portable"

# 本地 HTTP 阅读模式：静态根目录（同时提供 news-html/ 与 html/）
DEFAULT_EXPORT_ROOT = _PROJECT_ROOT / "data" / "export"

_QUICK_LIMITS = (50, 100, 200)
_DEFAULT_LIMIT = 100
_DEFAULT_AI_LIMIT = 3

_APP_TITLE = "Laxinwen News Reader"

# 来源选项：(内部 id, 显示名, 说明)
_SOURCE_OPTIONS = (
    ("eco", "ECO"),
    ("hkej", "HKEJ"),
    ("rfi", "RFI"),
    ("all", "全部"),
)
# 全部来源实际对应的站点 id（顺序保持：ECO 在前）
_ALL_SOURCE_IDS = ("eco", "hkej", "rfi")

# 后台线程完成哨兵 → 恢复提示文案
_DONE_SENTINELS = {
    "__FETCH_DONE__": "抓取",
    "__AI_DONE__": "AI 分析",
    "__ARCHIVE_DONE__": "打开新闻库",
    "__RESEARCH_DONE__": "打开 AI 研究结果",
    "__PORTABLE_EXPORT_DONE__": "导出",
}

# 导出方式选项：(内部 id, 显示名)
_EXPORT_OPTIONS = (
    ("reader", "📦 便携阅读包"),
    ("html", "📄 独立 HTML"),
    ("package", "📚 HTML 新闻包"),
)
# 默认导出方式 = 便携阅读包
_DEFAULT_EXPORT_MODE = "reader"


class _NewsReaderApp:
    """tkinter 主窗口。业务逻辑全部通过回调注入，便于离线测试。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        db_path: str | Path = DEFAULT_DB,
        site: str = "eco",
        site_name: str | None = None,
        storage_factory=None,
        pipeline_factory=None,
        processor_factory=None,
        news_archive_export=None,
        research_export=None,
        portable_html_export=None,
        portable_package_export=None,
        portable_reader_export=None,
        open_url=None,
        news_archive_dir: str | Path = DEFAULT_NEWS_ARCHIVE_DIR,
        research_dir: str | Path = DEFAULT_RESEARCH_DIR,
        portable_dir: str | Path = DEFAULT_PORTABLE_DIR,
        export_root: str | Path = DEFAULT_EXPORT_ROOT,
        server_factory=None,
        ai_config_store=None,
        ai_test_connection=None,
        ai_show_settings=None,
    ):
        self.root = root
        self.db_path = Path(db_path)
        self.site = site
        self.site_name = site_name or _site_display_name(site)
        self.news_archive_dir = Path(news_archive_dir)
        self.research_dir = Path(research_dir)
        self.portable_dir = Path(portable_dir)
        self.export_root = Path(export_root)

        # 依赖注入（默认走真实实现；测试可替换为假实现）
        self._server_factory = server_factory or _default_server_factory
        self._http_server = None
        self._server_lock = threading.Lock()
        self._storage_factory = storage_factory or _default_storage_factory
        self._pipeline_factory = pipeline_factory or _default_pipeline_factory
        self._processor_factory = processor_factory or _default_processor_factory
        self._news_archive_export = news_archive_export or _default_news_archive_export
        self._research_export = research_export or _default_research_export
        self._portable_html_export = portable_html_export or _default_portable_html_export
        self._portable_package_export = portable_package_export or _default_portable_package_export
        self._portable_reader_export = portable_reader_export or _default_portable_reader_export
        self._open_url = open_url or _default_open_url
        # 注意：_default_ai_config_store 是「返回 config_store 模块」的函数，
        # 必须调用它拿到模块对象，否则 self._ai_config_store 会变成函数对象，
        # 导致 .read_config()/.masked()/.apply_to_env() 触发
        # ``AttributeError: 'function' object has no attribute 'masked'``。
        self._ai_config_store = ai_config_store or _default_ai_config_store()
        self._ai_test_connection = ai_test_connection or _default_ai_test_connection
        self._ai_show_settings = ai_show_settings or _default_ai_show_settings

        # 后台线程 → GUI 消息队列
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._busy = False
        self._active_source = site
        self.last_action = "—"

        # 初始化时先建好数据库（含 schema），后续线程各自打开独立连接
        with self._storage_factory(self.db_path) as storage:
            init_ids = self._site_ids_for(site)
            self._last_fetch_at = self._read_last_fetch_at(storage, init_ids)
            self._analysis_status = self._read_analysis_status(storage, init_ids)

        self._build_ui()
        self._refresh_status()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ HTTP server

    def _ensure_http_server(self) -> None:
        """确保本地 HTTP 阅读服务器已启动（幂等，端口被占用自动换端口）。"""
        with self._server_lock:
            if self._http_server is not None:
                return
            server = self._server_factory(self.export_root)
            server.start()
            self._http_server = server
            self._bg_log(
                "本地 HTTP 阅读模式已启动（仅供本机访问，127.0.0.1）：\n"
                f"http://127.0.0.1:{server.port}/"
            )

    def _http_url_for(self, rel_path: str) -> str:
        """把相对路径转换为 http://127.0.0.1:<port>/<rel>（确保 server 已启动）。"""
        self._ensure_http_server()
        assert self._http_server is not None
        return self._http_server.url_for(rel_path)

    # ------------------------------------------------------------------ 来源

    def _site_ids_for(self, selection: str) -> tuple[str, ...]:
        """来源值 → 站点 id 列表（不依赖 site_var，用于初始化阶段）。"""
        if selection == "all":
            return _ALL_SOURCE_IDS
        if selection == "hkej":
            return ("hkej",)
        if selection == "rfi":
            return ("rfi",)
        return ("eco",)

    def _selected_site_ids(self) -> tuple[str, ...]:
        """返回当前来源对应的站点 id 列表（全部 → (eco, hkej, rfi)）。"""
        return self._site_ids_for(self.site_var.get())

    def _source_display(self, site_id: str) -> str:
        """站点 id → 显示名（ECO / HKEJ / 全部）。"""
        for sid, label in _SOURCE_OPTIONS:
            if sid == site_id:
                return label
        return site_id.upper()

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
        ttk.Label(row1, text="新闻来源：").pack(side="left")
        self.site_var = tk.StringVar(value=self.site)
        self.site_combo = ttk.Combobox(
            row1, textvariable=self.site_var, state="readonly", width=14
        )
        self.site_combo["values"] = [sid for sid, _ in _SOURCE_OPTIONS]
        self.site_combo.bind("<<ComboboxSelected>>", self._on_source_changed)
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

        self.ai_settings_btn = ttk.Button(
            row2, text="⚙ AI 设置", command=self._on_ai_settings
        )
        self.ai_settings_btn.pack(side="left", padx=(10, 0))

        # ---- 导出卡片（导出方式下拉 + 单一导出按钮） ----
        row3 = ttk.Frame(card)
        row3.pack(fill="x", pady=(10, 0))

        ttk.Label(row3, text="导出数量：").pack(side="left")
        self.export_limit_var = tk.StringVar(value=str(_DEFAULT_LIMIT))
        self.export_limit_entry = ttk.Entry(row3, textvariable=self.export_limit_var, width=8)
        self.export_limit_entry.pack(side="left", padx=4)

        ttk.Label(row3, text="导出方式：").pack(side="left", padx=(16, 4))
        # 默认 = 便携阅读包
        default_label = dict(_EXPORT_OPTIONS)[_DEFAULT_EXPORT_MODE]
        self.export_mode_var = tk.StringVar(value=default_label)
        self.export_mode_combo = ttk.Combobox(
            row3,
            textvariable=self.export_mode_var,
            state="readonly",
            width=16,
        )
        self.export_mode_combo["values"] = [label for _, label in _EXPORT_OPTIONS]
        self.export_mode_combo.pack(side="left", padx=(4, 10))

        self.export_btn = ttk.Button(
            row3, text="导出", style="Accent.TButton", command=self._on_export
        )
        self.export_btn.pack(side="left")

        # ---- 状态卡片 ----
        self.status_card = ttk.LabelFrame(outer, text="状态", padding=10)
        self.status_card.pack(fill="x", pady=(10, 0))
        self.status_labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(self.status_card)
        grid.pack(fill="x")
        for i, key in enumerate(
            ("db", "eco_count", "hkej_count", "rfi_count", "ai_ok", "ai_failed",
             "ai_status", "current_source", "last_action", "last_fetch")
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
            f"Laxinwen News Reader 已就绪 · 数据库：{self.db_path}\n"
            f"新闻来源：{self._source_display(self.site)}。"
            "发现→去重→下载→提取→入库 使用现有 pipeline，不会绕过去重逻辑。"
        )

    # ------------------------------------------------------------------ 工具

    def _set_limit(self, value: int) -> None:
        self.limit_var.set(str(value))

    def _on_source_changed(self, _event=None) -> None:
        """切换来源后刷新状态栏与当前来源显示。"""
        if self._busy:
            self.log("任务运行中，暂不支持切换来源，请等待完成。")
            # 恢复为任务开始前的来源
            self.site_var.set(self._active_source)
            return
        self.log(f"已切换新闻来源 → {self._source_display(self.site_var.get())}")
        self._refresh_status()

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
        self.ai_settings_btn.configure(state=state)
        self.site_combo.configure(state="disabled" if busy else "readonly")
        self.export_mode_combo.configure(state="disabled" if busy else "readonly")
        for e in (self.limit_entry, self.ai_limit_entry, self.export_limit_entry):
            e.configure(state=state)
        if busy:
            self._active_source = self.site_var.get()
            self.log(f"⏳ {run} 进行中，请稍候……")
        else:
            self.last_action = run
            self.log(f"✅ {run} 结束，按钮已恢复。")
            self._refresh_status()

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askokcancel(
                "退出", "后台任务仍在运行，确定要退出吗？"
            ):
                return
        # GUI 关闭时自动停止本地 HTTP 阅读服务器（生命周期跟随 GUI）
        with self._server_lock:
            if self._http_server is not None:
                try:
                    self._http_server.stop()
                except Exception as exc:
                    logging.getLogger("news.gui").warning("停止 HTTP 服务器失败: %s", exc)
                self._http_server = None
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

    def _read_last_fetch_at(self, storage, site_ids: tuple[str, ...]) -> Optional[str]:
        try:
            latest: Optional[datetime] = None
            for sid in site_ids:
                arts = storage.list_articles(source_id=sid, limit=1)
                if not arts:
                    continue
                fetched = arts[0].fetched_at or arts[0].discovered_at
                if fetched and (latest is None or fetched > latest):
                    latest = fetched
            return latest.isoformat() if latest else None
        except Exception as exc:  # 只读统计，失败不影响界面
            logging.getLogger("news.gui").warning("读取最后抓取时间失败: %s", exc)
            return None

    def _ai_status_label(self) -> str:
        """返回 AI 状态标签：🟢 已配置 / 🔴 配置存在但测试失败 / ⚪ 未配置。"""
        try:
            cfg = self._ai_config_store.read_config()
        except Exception:
            cfg = None
        if cfg is None or not cfg.is_complete():
            return "⚪ 未配置"
        # 有完整配置即视为已配置（测试失败通过“测试连接”状态单独提示）
        return "🟢 已配置"

    def _read_analysis_status(self, storage, site_ids: tuple[str, ...]) -> tuple[int, int]:
        try:
            ok = sum(storage.count_analysis(source_id=sid, status="success") for sid in site_ids)
            failed = sum(storage.count_analysis(source_id=sid, status="failed") for sid in site_ids)
            return ok, failed
        except Exception as exc:
            logging.getLogger("news.gui").warning("读取 AI 状态失败: %s", exc)
            return 0, 0

    # ------------------------------------------------------------------ 状态

    def _refresh_status(self) -> None:
        site_ids = self._selected_site_ids()
        try:
            with self._storage_factory(self.db_path) as storage:
                eco_count = storage.count(source_id="eco")
                hkej_count = storage.count(source_id="hkej")
                rfi_count = storage.count(source_id="rfi")
                self._last_fetch_at = self._read_last_fetch_at(storage, site_ids)
                self._analysis_status = self._read_analysis_status(storage, site_ids)
        except Exception as exc:
            self.log(f"状态读取失败：{exc}")
            eco_count = 0
            hkej_count = 0

        ai_ok, ai_failed = self._analysis_status
        current = self.site_var.get()
        values = {
            "db": f"数据库：{self.db_path}",
            "eco_count": f"ECO 新闻：{eco_count}",
            "hkej_count": f"HKEJ 新闻：{hkej_count}",
            "rfi_count": f"RFI 新闻：{rfi_count}",
            "ai_ok": f"AI 已分析：{ai_ok}",
            "ai_failed": f"AI 失败：{ai_failed}",
            "ai_status": f"AI 状态：{self._ai_status_label()}",
            "current_source": f"当前来源：{self._source_display(current)}",
            "last_action": f"最后操作：{self.last_action}",
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
        # 情况 B：AI 没有配置 → 提示用户进入 AI 设置（而不是只显示错误）
        if not self._is_ai_configured():
            self._prompt_configure_ai()
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

    def _selected_export_mode(self) -> str:
        """返回当前导出方式内部 id（reader / html / package）。"""
        label = self.export_mode_var.get()
        for mid, mlabel in _EXPORT_OPTIONS:
            if mlabel == label:
                return mid
        return _DEFAULT_EXPORT_MODE

    def _export_mode_label(self) -> str:
        for mid, label in _EXPORT_OPTIONS:
            if mid == self._selected_export_mode():
                return label
        return dict(_EXPORT_OPTIONS)[_DEFAULT_EXPORT_MODE]

    def _on_export(self) -> None:
        """【导出】统一入口 —— 根据「导出方式」下拉调用对应的现有导出器。

        底层三个导出器（portable_reader / portable_html / portable_package）
        全部保留，这里只是 GUI 层的统一入口（默认便携阅读包）。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        limit = self._parse_limit(self.export_limit_var.get(), what="导出")
        if limit is None:
            return
        mode = self._selected_export_mode()
        mode_label = self._export_mode_label()
        self._set_busy(True, run="导出")

        def worker() -> None:
            try:
                if mode == "reader":
                    self._run_export_portable_reader(limit)
                elif mode == "html":
                    self._run_export_independent_html(limit)
                else:
                    self._run_export_portable_package(limit)
            except Exception as exc:
                self._bg_log(f"导出（{mode_label}）失败：\n{exc}")
            finally:
                self._queue.put("__PORTABLE_EXPORT_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _is_ai_configured(self) -> bool:
        """判断 AI 是否已配置（基于配置中心当前读取到的完整配置）。"""
        try:
            cfg = self._ai_config_store.read_config()
        except Exception:
            cfg = None
        return bool(cfg is not None and cfg.is_complete())

    def _prompt_configure_ai(self) -> None:
        """情况 B：AI 未配置时提示用户进入 AI 设置（而非只显示错误）。"""
        if not messagebox.askyesno(
            "需要配置 AI",
            "当前尚未配置 AI。\n\n"
            "AI 分析需要：\n"
            "· Provider\n"
            "· API Base URL\n"
            "· API Key\n"
            "· Model\n\n"
            "是否现在打开 AI 设置？",
            parent=self.root,
        ):
            self.log("已取消 AI 分析（尚未配置 AI）。")
            return
        self._open_ai_settings()

    def _on_ai_settings(self) -> None:
        """【⚙ AI 设置】—— 打开独立的 AI 设置窗口。"""
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        self._open_ai_settings()

    def _open_ai_settings(self) -> None:
        """打开 AI 设置对话框；保存后立即刷新配置并更新状态。"""
        def _on_saved(cfg) -> None:
            # 保存后立即生效：把新配置同步到当前进程 os.environ（覆盖），
            # 供后续 AI 分析（AIProviderConfig.from_env()）立即读到，无需重启 GUI。
            try:
                self._ai_config_store.apply_to_env(cfg)
            except Exception:
                pass
            self.log(
                f"AI 配置已保存并立即生效：Provider={cfg.provider or '—'} "
                f"Model={cfg.model or '—'} API Key={self._ai_config_store.masked(cfg.api_key) or '未配置'}"
            )
            self._refresh_status()

        try:
            cfg = self._ai_config_store.read_config()
        except Exception:
            cfg = None
        self._ai_show_settings(self.root, current=cfg, on_save=_on_saved)

    def _bg_log(self, msg: str) -> None:
        """后台线程安全地追加日志。"""
        self._queue.put(msg)

    # ------------------------------------------------------------------ 业务

    def _run_fetch(self, limit: int) -> None:
        site_ids = self._selected_site_ids()
        self._bg_log(f"开始抓取 {self._source_display(self.site_var.get())} 最新 {limit} 篇")
        # 打开新连接（线程内使用）；多个站点共用一个 Storage 连接
        with self._storage_factory(self.db_path) as storage:
            pipeline = self._pipeline_factory(storage, limit)
            try:
                for sid in site_ids:
                    stats = pipeline.run_site(sid)
                    s = stats
                    self._bg_log(
                        f"{self._sep}\n"
                        f"[{self._source_display(sid)}] 发现：{s.discovered}\n"
                        f"重复：{s.skipped_dup}\n"
                        f"新增：{s.fetched_ok}\n"
                        f"失败：{s.failed}"
                    )
                    if s.errors:
                        self._bg_log(f"[{self._source_display(sid)}] 失败明细（前 5 条）：")
                        for err in s.errors[:5]:
                            self._bg_log(f"  - {err}")
            finally:
                try:
                    pipeline.close()
                except Exception:
                    pass
        self._bg_log(f"抓取完成（{self._source_display(self.site_var.get())}，limit={limit}）")

    def _run_ai_analyze(self, limit: int) -> None:
        site_ids = self._selected_site_ids()
        self._bg_log(f"开始 AI 分析（最多 {limit} 篇，复用现有 AI processing 逻辑）")
        with self._storage_factory(self.db_path) as storage:
            processor = self._processor_factory(storage)
            try:
                total_ok = 0
                total_failed = 0
                total_n = 0
                for sid in site_ids:
                    stats = processor.process_batch(source_id=sid, limit=limit)
                    total_n += stats.total
                    total_ok += stats.ok
                    total_failed += stats.failed
                    self._bg_log(
                        f"[{self._source_display(sid)}] AI 处理：共 {stats.total} 篇 | "
                        f"成功 {stats.ok} | 失败 {stats.failed}"
                    )
                    if stats.errors:
                        self._bg_log(f"[{self._source_display(sid)}] AI 失败明细（前 5 条）：")
                        for err in stats.errors[:5]:
                            self._bg_log(f"  - {err}")
            finally:
                try:
                    processor.close()
                except Exception:
                    pass
        self._bg_log(
            f"{self._sep}\n"
            f"AI 处理合计：共 {total_n} 篇 | 成功 {total_ok} | 失败 {total_failed}"
        )
        self._bg_log("AI 分析完成")

    def _run_news_archive(self, limit: int) -> None:
        site_ids = self._selected_site_ids()
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_dir = self.news_archive_dir / sid
                self._bg_log(f"正在导出 {self._source_display(sid)} News Archive（最近 {limit} 篇）→ {out_dir}")
                result = self._news_archive_export(
                    storage, out_dir, source_id=sid, limit=limit
                )
                index = result.index_path or out_dir / "index.html"
                if not index.exists():
                    raise FileNotFoundError(f"News Archive 未生成：{index}")
                self._bg_log(
                    f"{self._source_display(sid)} News Archive 导出完成：{result.exported} 篇"
                    f"（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）"
                )
                # 本地 HTTP 阅读模式：打开 http://127.0.0.1:<port>/news-html/<site>/index.html
                url = self._http_url_for(f"news-html/{sid}/index.html")
                self._bg_log(f"{self._source_display(sid)} 新闻库已启动：\n{url}")
                self._open_url(url)

    def _run_research(self) -> None:
        site_ids = self._selected_site_ids()
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_dir = self.research_dir / sid
                self._bg_log(f"正在导出 {self._source_display(sid)} AI 研究结果 HTML → {out_dir}")
                result = self._research_export(
                    storage, out_dir, source_id=sid
                )
                index = result.index_path or out_dir / "index.html"
                if not index.exists():
                    raise FileNotFoundError(f"AI 研究结果未生成：{index}")
                self._bg_log(
                    f"{self._source_display(sid)} AI 研究结果导出完成："
                    f"成功 {result.analysis_ok} / 失败 {result.analysis_failed}"
                )
                # 本地 HTTP 阅读模式：打开 http://127.0.0.1:<port>/html/<site>/index.html
                url = self._http_url_for(f"html/{sid}/index.html")
                self._bg_log(f"{self._source_display(sid)} AI 研究结果已启动：\n{url}")
                self._open_url(url)

    def _run_export_independent_html(self, limit: int) -> None:
        """导出独立 HTML（单个 self-contained 文件，双击可读，无需 laxinwen）。"""
        site_ids = self._selected_site_ids()
        research_root = self.research_dir
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_path = self.portable_dir / f"{sid}-{datetime.now().strftime('%Y-%m-%d')}.html"
                self._bg_log(
                    f"正在导出 {self._source_display(sid)} 独立 HTML（最近 {limit} 篇）→ {out_path}"
                )
                result = self._portable_html_export(
                    storage, out_path, source_id=sid, limit=limit, research_root=research_root
                )
                if not out_path.exists():
                    raise FileNotFoundError(f"独立 HTML 未生成：{out_path}")
                self._bg_log(
                    f"{self._source_display(sid)} 独立 HTML 导出完成：{result.exported} 篇"
                    f"（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）"
                )
                self._bg_log(f"📦 独立 HTML 文件：\n{out_path}")
                self._bg_log("可复制到没有 laxinwen 的电脑，双击即可阅读。")

    def _run_export_portable_package(self, limit: int) -> None:
        """导出 HTML 新闻包（可整体复制到其它电脑的独立阅读目录）。"""
        site_ids = self._selected_site_ids()
        research_root = self.research_dir
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_dir = self.portable_dir / f"{sid}-{datetime.now().strftime('%Y-%m-%d')}"
                self._bg_log(
                    f"正在导出 {self._source_display(sid)} HTML 新闻包（最近 {limit} 篇）→ {out_dir}"
                )
                result = self._portable_package_export(
                    storage, out_dir, source_id=sid, limit=limit, research_root=research_root
                )
                index = result.index_path or out_dir / "index.html"
                if not index.exists():
                    raise FileNotFoundError(f"HTML 新闻包未生成：{index}")
                self._bg_log(
                    f"{self._source_display(sid)} HTML 新闻包导出完成：{result.exported} 篇"
                    f"（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）"
                )
                self._bg_log(f"📚 HTML 新闻包目录：\n{out_dir}")
                self._bg_log("可复制整个目录到其它电脑，双击 index.html 阅读。")

    def _run_export_portable_reader(self, limit: int) -> None:
        """导出便携阅读包（给他人使用：双击 Open-Reader.bat，经 localhost 打开）。"""
        site_ids = self._selected_site_ids()
        research_root = self.research_dir
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_dir = self.portable_dir / f"Laxinwen-{sid.upper()}-{datetime.now().strftime('%Y-%m-%d')}"
                self._bg_log(
                    f"正在导出 {self._source_display(sid)} 便携阅读包（最近 {limit} 篇）→ {out_dir}"
                )
                result = self._portable_reader_export(
                    storage, out_dir, source_id=sid, limit=limit, research_root=research_root
                )
                bat = out_dir / "Open-Reader.bat"
                if not (out_dir / "index.html").exists() or not bat.exists():
                    raise FileNotFoundError(f"便携阅读包未生成：{out_dir}")
                self._bg_log(
                    f"{self._source_display(sid)} 便携阅读包导出完成：{result.exported} 篇"
                    f"（已分析 {result.analyzed_ok} / 失败 {result.analyzed_failed} / 未分析 {result.unanalyzed}）"
                )
                self._bg_log(f"📦 便携阅读包目录：\n{out_dir}")
                self._bg_log(
                    "给他人使用：复制整个目录，双击 Open-Reader.bat，"
                    "浏览器将通过 http://127.0.0.1 打开（而非 file://），"
                    "沉浸式翻译等扩展可正常工作。无需安装 laxinwen。"
                )


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


def _default_ai_config_store():
    """默认 AI 配置存取（.env 读写 / 掩码）。"""
    from .ai import config_store

    return config_store


def _default_ai_test_connection(config):
    """默认「测试连接」：真正调用一次极短模型请求。"""
    from .ai.provider import test_connection

    return test_connection(config)


def _default_ai_show_settings(parent, *, current=None, on_save=None):
    """默认 AI 设置窗口入口。"""
    from .ai_settings_dialog import show_ai_settings

    show_ai_settings(parent, current=current, on_save=on_save)


def _default_news_archive_export(storage, out_dir, *, source_id, limit):
    from .news_archive import export_news_archive

    return export_news_archive(storage, out_dir, source_id=source_id, limit=limit)


def _default_research_export(storage, out_dir, *, source_id):
    from .html_export import export_html

    return export_html(storage, out_dir, source_id=source_id)


def _default_portable_html_export(storage, out_path, *, source_id, limit, research_root=None):
    from .portable import export_independent_html

    return export_independent_html(
        storage, out_path, source_id=source_id, limit=limit, research_root=research_root
    )


def _default_portable_package_export(storage, out_dir, *, source_id, limit, research_root=None):
    from .portable import export_portable_package

    return export_portable_package(
        storage, out_dir, source_id=source_id, limit=limit, research_root=research_root
    )


def _default_portable_reader_export(storage, out_dir, *, source_id, limit, research_root=None):
    from .portable import export_portable_reader_package

    return export_portable_reader_package(
        storage, out_dir, source_id=source_id, limit=limit, research_root=research_root
    )


def _default_open_url(url: str) -> None:
    webbrowser.open(url)


def _default_server_factory(export_root: str | Path):
    """默认本地 HTTP 阅读服务器（仅 127.0.0.1，端口被占用自动换）。"""
    return ReaderServer(export_root)


# ---------- 启动入口 ----------


def run_gui(
    *,
    db_path: str | Path = DEFAULT_DB,
    site: str = "eco",
) -> int:
    """启动 tkinter 主循环（供 ``news gui`` 与 NewsReader.bat 调用）。

    ``site`` 为初始来源：eco / hkej / all，运行中可在 GUI 内切换。
    """
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
    for sid, label in _SOURCE_OPTIONS:
        if sid == site:
            return label
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
        description="Laxinwen News Reader —— 轻量级 Windows 桌面 GUI",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument(
        "--site", default="eco", help="初始来源 id：eco / hkej / all（默认 eco）"
    )
    args = parser.parse_args(argv)
    return run_gui(db_path=args.db, site=args.site)


if __name__ == "__main__":
    sys.exit(main())
