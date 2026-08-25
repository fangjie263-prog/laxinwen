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
import json
import os
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import messagebox, ttk

from .reader_server import ReaderServer

# 定时抓取相关（延迟导入默认实现，便于测试注入）
from .scheduler_config import (
    EXPORT_BOTH,
    EXPORT_PORTABLE,
    EXPORT_WORD,
    FREQ_DAILY,
    FREQ_HOURLY,
    HOURLY_INTERVALS,
    SchedulerConfig,
    load_config as _default_scheduler_load,
    load_jobs as _default_scheduler_load_jobs,
    save_config as _default_scheduler_save,
    save_jobs as _default_scheduler_save_jobs,
)
from .task_scheduler import (
    delete_task as _default_task_delete,
    install_task as _default_task_install,
    query_task as _default_task_query,
    run_now as _default_task_run_now,
    enable_task as _default_task_enable,
    disable_task as _default_task_disable,
)


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

# 自动抓取后台日志
DEFAULT_SCHED_LOG = _PROJECT_ROOT / "data" / "logs" / "scheduled-fetch.log"


def _scheduled_log_path() -> Path:
    """返回自动抓取后台日志路径。"""
    return DEFAULT_SCHED_LOG

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
    "__NOTION_SYNC_DONE__": "Notion 同步",
    "__SCHED_INSTALL_DONE__": "安装/更新定时任务",
    "__SCHED_DELETE_DONE__": "删除定时任务",
    "__SCHED_RUNNOW_DONE__": "立即运行一次",
    "__SCHED_TOGGLE_DONE__": "启用/停用",
}

# 导出方式：仅提供「便携阅读包」单一入口（= Portable HTML + Word DOCX 一次生成）。
# 用户无需选择内部 export_type（portable / word / both），GUI 不再暴露这些枚举。
_EXPORT_OPTIONS = (
    ("reader", "📦 便携阅读包（HTML + Word）"),
)
# 默认导出方式 = 便携阅读包
_DEFAULT_EXPORT_MODE = "reader"

# 定时任务自动导出格式选项：(内部 id, 显示名)
# 为向后兼容旧 scheduler.json 的 export_type 字段而保留（不暴露给 GUI 下拉）；
# 运行时统一解释为 HTML + DOCX（见 scheduled_fetch._run_auto_export）。
_EXPORT_TYPE_OPTIONS = (
    (EXPORT_PORTABLE, "Portable HTML"),
    (EXPORT_WORD, "Word"),
    (EXPORT_BOTH, "HTML + Word"),
)


def _export_type_label(export_type: str) -> str:
    """导出类型内部 id → 显示名（未知值安全回退为 Portable HTML）。"""
    for tid, label in _EXPORT_TYPE_OPTIONS:
        if tid == export_type:
            return label
    return dict(_EXPORT_TYPE_OPTIONS)[EXPORT_PORTABLE]


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
        word_export=None,
        open_url=None,
        news_archive_dir: str | Path = DEFAULT_NEWS_ARCHIVE_DIR,
        research_dir: str | Path = DEFAULT_RESEARCH_DIR,
        portable_dir: str | Path = DEFAULT_PORTABLE_DIR,
        export_root: str | Path = DEFAULT_EXPORT_ROOT,
        server_factory=None,
        ai_config_store=None,
        ai_test_connection=None,
        ai_show_settings=None,
        scheduler_load=None,
        scheduler_save=None,
        scheduler_load_jobs=None,
        scheduler_save_jobs=None,
        scheduler_install=None,
        scheduler_delete=None,
        scheduler_run_now=None,
        scheduler_query=None,
        scheduler_enable=None,
        scheduler_disable=None,
        scheduler_config_path=None,
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
        self._word_export = word_export or _default_word_export
        self._open_url = open_url or _default_open_url
        # 注意：_default_ai_config_store 是「返回 config_store 模块」的函数，
        # 必须调用它拿到模块对象，否则 self._ai_config_store 会变成函数对象，
        # 导致 .read_config()/.masked()/.apply_to_env() 触发
        # ``AttributeError: 'function' object has no attribute 'masked'``。
        self._ai_config_store = ai_config_store or _default_ai_config_store()
        self._ai_test_connection = ai_test_connection or _default_ai_test_connection
        self._ai_show_settings = ai_show_settings or _default_ai_show_settings

        # 定时抓取：配置存取 + Task Scheduler 操作（可注入假实现便于测试）
        self._scheduler_load = scheduler_load or _default_scheduler_load
        self._scheduler_save = scheduler_save or _default_scheduler_save
        self._scheduler_load_jobs = scheduler_load_jobs or _default_scheduler_load_jobs
        self._scheduler_save_jobs = scheduler_save_jobs or _default_scheduler_save_jobs
        self._scheduler_install = scheduler_install or _default_task_install
        self._scheduler_delete = scheduler_delete or _default_task_delete
        self._scheduler_run_now = scheduler_run_now or _default_task_run_now
        self._scheduler_query = scheduler_query or _default_task_query
        self._scheduler_enable = scheduler_enable or _default_task_enable
        self._scheduler_disable = scheduler_disable or _default_task_disable
        self._scheduler_config_path = (
            Path(scheduler_config_path) if scheduler_config_path else None
        )
        # 当前定时任务列表（多任务 GUI 状态）
        self._scheduler_jobs = self._scheduler_load_jobs(self._scheduler_config_path)
        for job in self._scheduler_jobs:
            if job.source not in ("rfi", "eco", "hkej"):
                job.source = "rfi"
        # 兼容旧单任务配置：若 jobs 为空但旧配置存在，则回退到旧配置
        if not self._scheduler_jobs:
            legacy = self._scheduler_load(self._scheduler_config_path)
            if legacy and legacy.source in ("rfi", "eco", "hkej"):
                legacy.auto_export = True
                self._scheduler_jobs = [legacy]
        self._selected_job = None
        # Windows Task Scheduler 真实状态缓存：{job_id: dict|None}
        # dict 含 keys: exists / enabled / running；None 表示查询失败/未知。
        self._sched_win_state: dict[str, Optional[dict]] = {}
        # 最近一次「安装/更新」失败过的 job_id 集合（用于显示「安装失败」）。
        self._sched_install_failed: set[str] = set()

        # 后台线程 → GUI 消息队列
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._busy = False
        self._active_source = site
        self.last_action = "—"
        self.notion_status_var = tk.StringVar(value="")

        # 抓取监控（小窗口摘要）：只保留最近若干条任务级摘要，不显示底层日志
        self._monitor_entries: list[str] = []
        self._monitor_cur: str = "空闲"
        # 当前正在累积解析的一个任务（实时链路用状态机累积各阶段数字）
        self._monitor_task: dict | None = None
        # 已从 scheduled-fetch.log 消费到的行位置（用于 GUI 打开时增量读取后台完成结果）
        self._monitor_sched_tail_pos: int = 0
        self._monitor_poll_count: int = 0

        # 初始化时先建好数据库（含 schema），后续线程各自打开独立连接
        with self._storage_factory(self.db_path) as storage:
            init_ids = self._site_ids_for(site)
            self._last_fetch_at = self._read_last_fetch_at(storage, init_ids)
            self._analysis_status = self._read_analysis_status(storage, init_ids)

        self._build_ui()
        self._apply_scheduler_to_ui()
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

        # ---- 导出卡片（单一「导出便携阅读包」按钮 = HTML + Word 一次生成） ----
        # 导出数量统一使用顶部 limit_var（顶部“最近 N 篇”是唯一权威 limit）。
        row3 = ttk.Frame(card)
        row3.pack(fill="x", pady=(10, 0))

        self.export_btn = ttk.Button(
            row3, text="导出便携阅读包（HTML + Word）", style="Accent.TButton",
            command=self._on_export,
        )
        self.export_btn.pack(side="left")
        ttk.Label(
            row3,
            text="一次生成 HTML 阅读包 + Word 研究阅读包",
            foreground="#666",
        ).pack(side="left", padx=(8, 0))
        self.notion_sync_btn = ttk.Button(
            row3, text="Notion 同步", command=self._on_notion_sync
        )
        self.notion_sync_btn.pack(side="left", padx=(12, 0))
        ttk.Label(
            row3, textvariable=self.notion_status_var, foreground="#666"
        ).pack(side="left", padx=(8, 0))
        self._refresh_notion_status()

        # ---- 自动抓取 / 定时任务卡片（多任务列表） ----
        sched_card = ttk.LabelFrame(outer, text="自动抓取 / 定时任务", padding=10)
        sched_card.pack(fill="x", pady=(10, 0))

        # 提示：自动导出为固定行为
        ttk.Label(
            sched_card,
            text="每个定时任务完成后自动生成便携阅读包（自动导出固定启用，无需手动开关）。",
            foreground="#666",
        ).pack(anchor="w")

        # 任务列表
        self.sched_tree = ttk.Treeview(
            sched_card,
            columns=("name", "source", "freq", "limit", "status"),
            show="headings",
            height=5,
        )
        self.sched_tree.heading("name", text="任务名称")
        self.sched_tree.heading("source", text="来源")
        self.sched_tree.heading("freq", text="频率")
        self.sched_tree.heading("limit", text="数量")
        self.sched_tree.heading("status", text="状态")
        self.sched_tree.column("name", width=160, anchor="w")
        self.sched_tree.column("source", width=60, anchor="center")
        self.sched_tree.column("freq", width=110, anchor="center")
        self.sched_tree.column("limit", width=60, anchor="center")
        self.sched_tree.column("status", width=70, anchor="center")
        self.sched_tree.pack(fill="x")
        self.sched_tree.bind("<<TreeviewSelect>>", self._on_sched_select)

        sbtn = ttk.Frame(sched_card)
        sbtn.pack(fill="x", pady=(8, 0))
        self.sched_new_btn = ttk.Button(
            sbtn, text="新建任务", command=self._on_sched_new
        )
        self.sched_new_btn.pack(side="left")
        self.sched_edit_btn = ttk.Button(
            sbtn, text="编辑", command=self._on_sched_edit
        )
        self.sched_edit_btn.pack(side="left", padx=(6, 0))
        self.sched_toggle_btn = ttk.Button(
            sbtn, text="启用/停用", command=self._on_sched_toggle
        )
        self.sched_toggle_btn.pack(side="left", padx=(6, 0))
        self.sched_runnow_btn = ttk.Button(
            sbtn, text="立即运行一次", command=self._on_sched_run_now
        )
        self.sched_runnow_btn.pack(side="left", padx=(6, 0))
        self.sched_install_btn = ttk.Button(
            sbtn, text="安装/更新", command=self._on_sched_install
        )
        self.sched_install_btn.pack(side="left", padx=(6, 0))
        self.sched_delete_btn = ttk.Button(
            sbtn, text="删除", command=self._on_sched_delete
        )
        self.sched_delete_btn.pack(side="left", padx=(6, 0))

        # 调度状态显示
        self.sched_status_var = tk.StringVar(value="自动抓取：未启用")
        ttk.Label(sched_card, textvariable=self.sched_status_var).pack(
            anchor="w", pady=(6, 0)
        )

        # ---- 抓取监控（小窗口摘要） ----
        monitor_card = ttk.LabelFrame(outer, text="抓取监控", padding=8)
        monitor_card.pack(fill="x", pady=(10, 0))
        monitor_head = ttk.Frame(monitor_card)
        monitor_head.pack(fill="x")
        ttk.Label(
            monitor_head,
            text="当前：",
            foreground="#666",
        ).pack(side="left")
        self.monitor_cur_var = tk.StringVar(value="空闲")
        ttk.Label(
            monitor_head, textvariable=self.monitor_cur_var, font=("", 10, "bold")
        ).pack(side="left")
        self.monitor_clear_btn = ttk.Button(
            monitor_head, text="清空", width=6, command=self._on_monitor_clear
        )
        self.monitor_clear_btn.pack(side="right")
        self.monitor_text = tk.Text(
            monitor_card,
            height=5,
            state="disabled",
            wrap="none",
            font=("Consolas", 9),
        )
        self.monitor_text.pack(side="left", fill="x", pady=(4, 0))
        mscroll = ttk.Scrollbar(
            monitor_card, command=self.monitor_text.yview
        )
        mscroll.pack(side="right", fill="y")
        self.monitor_text.configure(yscrollcommand=mscroll.set)

        # ---- 状态卡片 ----
        self.status_card = ttk.LabelFrame(outer, text="状态", padding=10)
        self.status_card.pack(fill="x", pady=(10, 0))
        self.status_labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(self.status_card)
        grid.pack(fill="x")
        for i, key in enumerate(
            ("db", "eco_count", "hkej_count", "rfi_count", "usable", "ai_ok", "ai_failed",
             "ai_status", "current_source", "last_action", "last_fetch")
        ):
            lbl = ttk.Label(grid, text="")
            lbl.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 24), pady=1)
            self.status_labels[key] = lbl

        # ---- 抓取进度 / 状态摘要 ----
        fetch_card = ttk.LabelFrame(outer, text="当前抓取状态", padding=8)
        fetch_card.pack(fill="x", pady=(10, 0))
        self.fetch_status_var = tk.StringVar(value="状态：空闲")
        ttk.Label(
            fetch_card, textvariable=self.fetch_status_var, font=("", 10, "bold")
        ).pack(anchor="w")

        # ---- 日志区 ----------------
        log_card = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_card.pack(fill="both", expand=True, pady=(10, 0))

        # 日志区标题栏（含“清空当前显示”，只清 GUI 显示，不删真实日志文件）
        log_head = ttk.Frame(log_card)
        log_head.pack(fill="x")
        ttk.Label(
            log_head,
            text="抓取/定时任务实时进度（最近 200 行）。",
            foreground="#666",
        ).pack(side="left")
        self.log_clear_btn = ttk.Button(
            log_head, text="清空当前显示", width=14, command=self._on_log_clear
        )
        self.log_clear_btn.pack(side="right")

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
        self.log_text.pack(side="left", fill="both", expand=True, pady=(4, 0))
        scroll = ttk.Scrollbar(log_card, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

        # 最近一次抓取统计（状态摘要用）
        self._last_fetch_summary: dict = {
            "source": self._source_display(self.site),
            "limit": 0,
            "discovered": 0,
            "duplicated": 0,
            "usable": 0,
            "failed": 0,
            "export": "—",
            "status": "空闲",
        }

        self._sep = "─" * 58
        self.log(
            f"Laxinwen News Reader 已就绪 · 数据库：{self.db_path}\n"
            f"新闻来源：{self._source_display(self.site)}。"
            "发现→去重→下载→提取→入库 使用现有 pipeline，不会绕过去重逻辑。"
        )
        # 启动时读取最近一次后台 scheduled-fetch.log（若存在）
        self._load_recent_scheduled_log()

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

    # 日志区最多保留的行数（超出自动裁剪最旧的行，只影响 GUI 显示）
    _LOG_MAX_LINES = 200

    def _on_log_clear(self) -> None:
        """清空当前 GUI 日志显示（不删除真实日志文件）。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_bg_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        # 裁剪到最近 _LOG_MAX_LINES 行，避免日志区无限增长
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > self._LOG_MAX_LINES:
            remove = line_count - self._LOG_MAX_LINES
            self.log_text.delete(f"1.0", f"{remove + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ========================================================== 抓取监控

    # 抓取监控小窗口最多保留的任务级摘要条数
    _MONITOR_MAX = 8

    def _on_monitor_clear(self) -> None:
        """清空抓取监控小窗口当前显示（只清 GUI，不删除 scheduled-fetch.log）。"""
        self._monitor_entries = []
        self._monitor_cur = "空闲"
        self.monitor_cur_var.set("空闲")
        self._monitor_render()

    def _monitor_render(self) -> None:
        """把监控摘要列表渲染到小窗口 Text（主线程调用）。"""
        self.monitor_text.configure(state="normal")
        self.monitor_text.delete("1.0", "end")
        for line in self._monitor_entries[-self._MONITOR_MAX:]:
            self.monitor_text.insert("end", line + "\n")
        self.monitor_text.configure(state="disabled")
        self.monitor_text.see("end")

    def _monitor_add_entry(self, text: str) -> None:
        """追加一条任务级摘要到小窗口并保留（不清空）。"""
        stamp = datetime.now().strftime("%H:%M")
        self._monitor_entries.append(f"{stamp}  {text}")
        self._monitor_render()

    def _monitor_set_cur(self, text: str) -> None:
        """更新“当前：”任务摘要。"""
        self._monitor_cur = text
        self.monitor_cur_var.set(text)

    @staticmethod
    def _monitor_source_label(source: str) -> str:
        """把来源 id/显示名归一化为大写短名（RFI / HKEJ / ECO / 全部）。"""
        s = (source or "").strip().upper()
        return s or "?"

    @staticmethod
    def _monitor_int(text: str):
        """从一段文本里提取第一个整数，取不到返回 None。"""
        import re
        m = re.search(r"-?\d+", text)
        return int(m.group()) if m else None

    def _monitor_identity(self, task: dict) -> str:
        """生成任务身份串：`JOB · SOURCE`。

        JOB 是任务身份（如 rfi-morning / rfi-hourly），SOURCE 是新闻来源
        （RFI / HKEJ / ECO）。两者都可能缺失：
        - 后台任务两者都有 → 显示 `test · ECO`；
        - 手动抓取只有来源 → 显示 `ECO`；
        - 只有 JOB 没有 SOURCE → 显示 `test`；
        - 都缺失 → 回退为“任务”（绝不显示裸 `?`）。
        """
        job = (task.get("job") or "").strip()
        src = (task.get("source") or "").strip()
        label = self._monitor_source_label(src)
        if job and label and label != "?":
            return f"{job} · {label}"
        if job:
            return job
        if label and label != "?":
            return label
        return "任务"

    @staticmethod
    def _parse_export_detail(line: str) -> str:
        """从 EXPORT 日志行提取 HTML / Word 分项结果。

        新日志格式：``EXPORT: FAILED → HTML: SUCCESS / WORD: FAILED``。
        从中提取 ``HTML: X`` 与 ``WORD: Y`` 用于监控文案；解析不到时回退
        为统一的「HTML + Word」描述。
        """
        m = re.search(r"HTML:\s*(SUCCESS|FAILED)\s*/\s*WORD:\s*(SUCCESS|FAILED)", line)
        if m:
            html = m.group(1)
            word = m.group(2)
            if html == "SUCCESS" and word == "SUCCESS":
                return "HTML + Word 导出成功"
            if html == "SUCCESS" and word == "FAILED":
                return "Word 导出失败（HTML 成功）"
            if html == "FAILED" and word == "SUCCESS":
                return "HTML 导出失败（Word 成功）"
            return "HTML + Word 导出失败"
        if "EXPORT: SUCCESS" in line:
            return "HTML + Word 导出成功"
        return "导出失败"

    def _monitor_finalize(self, *, failed: bool = False) -> None:
        """根据累积的 _monitor_task 生成一条完成摘要并显示。"""
        task = self._monitor_task
        self._monitor_task = None
        if not task:
            return
        ident = self._monitor_identity(task)
        limit = task.get("limit")
        discovered = task.get("discovered")
        usable = task.get("usable")
        export = task.get("export")

        if failed:
            self._monitor_set_cur(f"{ident} · 抓取失败")
            self._monitor_add_entry(f"{ident}  抓取失败")
            return

        # “无新文章”不是失败（usable=0 / discovered=0 但 FETCH SUCCESS）
        if usable == 0 or (discovered == 0 and usable in (None, 0)):
            new_txt = "无新文章"
        else:
            new_n = usable if usable is not None else discovered
            new_txt = f"新增 {new_n} 条" if new_n is not None else "已完成"
        # 优先使用分项文案（HTML + Word 导出成功 / Word 导出失败…），
        # 回退到统一文案。
        export_detail = task.get("export_detail")
        if export_detail:
            export_txt = f"，{export_detail}"
        else:
            export_txt = {None: "", "成功": "，HTML + Word 导出成功",
                          "失败": "，导出失败", "跳过": "，未导出"}.get(export, "")
        # 导出失败也算完成，但结果提示失败（FETCH 与 EXPORT 保持独立）
        self._monitor_set_cur(f"{ident} · 已完成 · {new_txt}{export_txt}")
        if limit is not None:
            self._monitor_add_entry(
                f"{ident}  完成：{new_txt}（目标 {limit} 条）{export_txt}"
            )
        else:
            self._monitor_add_entry(f"{ident}  完成：{new_txt}{export_txt}")

    def _monitor_feed_line(self, line: str) -> None:
        """实时链路：喂入一行日志，更新监控状态机。主线程调用。

        兼容手动抓取（queue）与后台任务（scheduled-fetch.log）两种日志格式。
        """
        line = (line or "").strip()
        if not line:
            return
        task = self._monitor_task

        # —— 开始事件 ——
        # 手动："开始抓取 RFI 最新 50 篇"
        if "开始抓取" in line and "最新" in line:
            src = line.split("开始抓取", 1)[1].split("最新", 1)[0].strip()
            limit = self._monitor_int(line.split("最新", 1)[1])
            self._monitor_task = {"source": src, "limit": limit}
            self._monitor_set_cur(
                f"{self._monitor_source_label(src)} · 抓取中……"
            )
            label = self._monitor_source_label(src)
            if limit is not None:
                self._monitor_add_entry(f"{label}  开始抓取，目标 {limit} 条")
            else:
                self._monitor_add_entry(f"{label}  开始抓取")
            return
        # 后台：JOB（任务身份）+ SOURCE（新闻来源），来自 scheduled-fetch.log
        if line.startswith("JOB:"):
            job = line.split("JOB:", 1)[1].strip()
            if task is None:
                task = self._monitor_task = {}
            task["job"] = job
            return
        # 后台："SOURCE: rfi"（仅当尚未记录来源时设置）
        if line.startswith("SOURCE:") and not (task and task.get("source")):
            src = line.split("SOURCE:", 1)[1].strip()
            if task is None:
                task = self._monitor_task = {}
            task["source"] = src
            self._monitor_set_cur(
                f"{self._monitor_identity(task)} · 抓取中……"
            )
            return
        if not task:
            return

        # —— 阶段数字 ——
        if "发现数量:" in line:
            n = self._monitor_int(line.split("发现数量:", 1)[1])
            if n is not None:
                task["discovered"] = n
        elif "合计发现" in line:
            # 手动多来源抓取的合计："合计发现 N 篇"
            n = self._monitor_int(line.split("合计发现", 1)[1])
            if n is not None:
                task["discovered"] = n
        elif "发现：" in line:
            n = self._monitor_int(line.split("发现：", 1)[1])
            if n is not None:
                task["discovered"] = n
        if "可读新闻:" in line or "可读新闻：" in line:
            seg = line.split("可读新闻", 1)[1]
            n = self._monitor_int(seg)
            if n is not None:
                task["usable"] = n
        elif "合计发现" in line:
            # 手动多来源抓取合计行里同时含可读数："... 可读 N / 目标 L ..."
            seg = line.split("可读", 1)[1] if "可读" in line else None
            if seg is not None:
                n = self._monitor_int(seg)
                if n is not None:
                    task["usable"] = n
        if "TARGET:" in line:
            n = self._monitor_int(line.split("TARGET:", 1)[1])
            if n is not None:
                task["limit"] = n

        # —— 结果 ——
        if "FETCH: FAILED" in line:
            task["fetch_failed"] = True
            self._monitor_finalize(failed=True)
            return
        if "FETCH: SUCCESS" in line:
            task["fetch_ok"] = True
            return
        if "EXPORT: SUCCESS" in line:
            task["export"] = "成功"
            task["export_detail"] = self._parse_export_detail(line)
            return
        if "EXPORT: FAILED" in line:
            task["export"] = "失败"
            task["export_detail"] = self._parse_export_detail(line)
            return
        if "EXPORT: SKIPPED" in line:
            task["export"] = "跳过"
            task["export_detail"] = "未导出"
            return
        if "自动定时抓取异常（ERROR）" in line or (
            "抓取失败" in line and task.get("fetch_ok") is not True
        ):
            self._monitor_finalize(failed=True)
            return
        # 结束标记：手动 "抓取完成" / 后台 "结束"
        if "抓取完成" in line or ("自动定时抓取结束" in line):
            self._monitor_finalize()
            return

    def _monitor_parse_sched_log(self, lines: list[str]) -> None:
        """解析 scheduled-fetch.log 的最近若干段任务，生成任务级摘要。

        每段以“自动定时抓取开始”到“自动定时抓取结束”为界。只把最近 N 段
        摘要填进监控小窗口，不显示底层日志。
        """
        self._monitor_task = None
        for line in lines:
            if "自动定时抓取开始" in line:
                # 新任务段落：先终结上一个未结束任务（防御）
                if self._monitor_task:
                    self._monitor_finalize()
                self._monitor_task = {"phase": "started"}
            self._monitor_feed_line(line)
        # 末尾若还有未终结任务，归一化为完成
        if self._monitor_task:
            self._monitor_finalize()
        if not self._monitor_entries:
            self._monitor_set_cur("空闲")

    def _monitor_poll_sched_log(self) -> None:
        """GUI 打开期间增量读取 scheduled-fetch.log 新增行，实时反映后台任务完成。

        只读取新增部分（不重放已消费行），后台任务每完成一段即把摘要推进小窗口。
        """
        try:
            p = _scheduled_log_path()
            if not p.is_file():
                return
            lines = p.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except Exception:
            return
        if len(lines) <= self._monitor_sched_tail_pos:
            return
        new_lines = lines[self._monitor_sched_tail_pos:]
        self._monitor_sched_tail_pos = len(lines)
        for line in new_lines:
            if "自动定时抓取开始" in line:
                # 新任务段落：先终结上一个未结束任务（防御）
                if self._monitor_task:
                    self._monitor_finalize()
                self._monitor_task = {"phase": "started"}
            self._monitor_feed_line(line)


    def _load_recent_scheduled_log(self, max_lines: int = 200) -> None:
        """GUI 启动时读取最近一次后台 scheduled-fetch.log 记录并显示。

        只读取日志文件、只读显示，不删除真实日志。文件不存在 / 为空时静默跳过。
        """
        try:
            p = _scheduled_log_path()
            if not p.is_file():
                return
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            self.log(f"读取后台日志失败：{exc}")
            return
        if not lines:
            return
        # 后台任务摘要：读取全文以恢复最近的“任务级摘要”（不依赖 tail 截断）
        try:
            self._monitor_parse_sched_log(lines)
        except Exception:
            pass
        # 已消费全文，之后增量轮询从当前末尾继续
        self._monitor_sched_tail_pos = len(lines)
        self._sep = "─" * 58
        self.log(self._sep)
        self.log("最近一次后台定时任务运行记录（data/logs/scheduled-fetch.log）：")
        tail = lines[-max_lines:]
        for line in tail:
            self.log(line)
        self._update_fetch_summary_from_log(tail)
        self.log(self._sep)

    def _update_fetch_summary_from_log(self, lines: list[str]) -> None:
        """从后台日志行提取最近一次抓取的关键统计，更新状态摘要栏。"""
        summary = dict(self._last_fetch_summary)
        summary["status"] = "已完成"
        fetch_ok = False
        export = None
        export_detail = None
        target = None
        discovered = None
        usable = None
        duplicated = None
        failed = None
        source = None
        for line in lines:
            l = line
            if "SOURCE:" in l and source is None:
                parts = l.split("SOURCE:", 1)
                if len(parts) > 1:
                    source = parts[1].strip()
            if "TARGET:" in l and target is None:
                parts = l.split("TARGET:", 1)
                if len(parts) > 1:
                    target = self._extract_int(parts[1])
            if "发现数量:" in l and discovered is None:
                discovered = self._extract_int(l.split("发现数量:", 1)[-1])
            if "重复数量:" in l and duplicated is None:
                duplicated = self._extract_int(l.split("重复数量:", 1)[-1])
            if "可读新闻:" in l and usable is None:
                usable = self._extract_int(l.split("可读新闻:", 1)[-1])
            if "抓取/提取失败:" in l and failed is None:
                failed = self._extract_int(l.split("抓取/提取失败:", 1)[-1])
            if "FETCH: SUCCESS" in l:
                fetch_ok = True
            if "FETCH: FAILED" in l:
                fetch_ok = False
            if "EXPORT: SUCCESS" in l:
                export = "成功"
                export_detail = self._parse_export_detail(l)
            elif "EXPORT: FAILED" in l:
                export = "失败"
                export_detail = self._parse_export_detail(l)
            elif "EXPORT: SKIPPED" in l:
                export = "跳过"
                export_detail = "未导出"
        if fetch_ok is False and "FETCH:" not in "".join(lines):
            fetch_ok = True
        summary["source"] = source or summary["source"]
        if target is not None:
            summary["limit"] = target
        if discovered is not None:
            summary["discovered"] = discovered
        if duplicated is not None:
            summary["duplicated"] = duplicated
        if usable is not None:
            summary["usable"] = usable
        if failed is not None:
            summary["failed"] = failed
        if export is not None:
            summary["export"] = export
        if export_detail is not None:
            summary["export_detail"] = export_detail
        self._last_fetch_summary = summary
        self._render_fetch_status()

    @staticmethod
    def _extract_int(text: str):
        import re

        m = re.search(r"-?\d+", text)
        return int(m.group()) if m else None

    def _update_fetch_summary(self, *, source, limit, discovered, duplicated, usable,
                              failed, export, status) -> None:
        """手动抓取后更新状态摘要栏。"""
        self._last_fetch_summary = {
            "source": source,
            "limit": limit,
            "discovered": discovered,
            "duplicated": duplicated,
            "usable": usable,
            "failed": failed,
            "export": export,
            "status": status,
        }
        self._render_fetch_status()

    def _render_fetch_status(self) -> None:
        """渲染抓取状态摘要文本（GUI 主线程调用）。"""
        s = self._last_fetch_summary
        status = s["status"]
        parts = [f"状态：{status}"]
        export_txt = s.get("export_detail") or s.get("export") or ""
        detail = (
            f"{s['source']} · 目标 {s['limit']} · 发现 {s['discovered']} · "
            f"重复 {s['duplicated']} · 可读 {s['usable']} · 失败 {s['failed']} · 导出 {export_txt}"
        )
        self.fetch_status_var.set("    ".join(parts) + "    " + detail)

    def _poll_queue(self) -> None:
        """主线程轮询后台日志队列，避免跨线程操作 Tk 控件。"""
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg in _DONE_SENTINELS:
                    self._set_busy(False, run=_DONE_SENTINELS[msg])
                    if msg.startswith("__SCHED"):
                        # 定时任务操作完成后：重新查询 Windows 状态并刷新列表/汇总
                        try:
                            self._refresh_windows_task_state()
                        except Exception:
                            pass
                        self._refresh_scheduler_table()
                    elif msg == "__FETCH_DONE__":
                        # 手动抓取完成后刷新顶部抓取状态摘要（worker 已更新 dict）
                        try:
                            self._render_fetch_status()
                        except Exception:
                            pass
                        # 防御：若手动抓取异常导致未 finalize，强制收尾
                        if self._monitor_task:
                            self._monitor_finalize()
                    elif msg == "__NOTION_SYNC_DONE__":
                        self._refresh_notion_status()
                    self._refresh_status()
                else:
                    self.log(msg)
                    # 逐行喂给抓取监控解析器（只提取任务级摘要）
                    for _ln in str(msg).splitlines():
                        self._monitor_feed_line(_ln)
                        # 「立即运行一次」：只显示“已请求执行”，不误判为抓取成功
                        if "已请求 Windows Task Scheduler 执行任务" in _ln:
                            job_id = _ln.split("执行任务：", 1)[1].strip()
                            self._monitor_add_entry(
                                f"{job_id}  已请求 Windows Task Scheduler 执行"
                            )
                            self._monitor_set_cur(f"{job_id} · 已请求执行，等待后台完成")
        except queue.Empty:
            pass
        # 每约 3 秒增量读取后台 scheduled-fetch.log，反映后台任务完成结果
        self._monitor_poll_count += 1
        if self._monitor_poll_count >= 30:
            self._monitor_poll_count = 0
            try:
                self._monitor_poll_sched_log()
            except Exception:
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
        self.notion_sync_btn.configure(state=state)
        self.site_combo.configure(state="disabled" if busy else "readonly")
        for e in (self.limit_entry, self.ai_limit_entry):
            e.configure(state=state)
        # 定时任务按钮随忙碌状态禁用
        for btn in (
            self.sched_new_btn,
            self.sched_edit_btn,
            self.sched_toggle_btn,
            self.sched_install_btn,
            self.sched_delete_btn,
            self.sched_runnow_btn,
        ):
            btn.configure(state=state)
        if busy:
            self._active_source = self.site_var.get()
            self.log(f"⏳ {run} 进行中，请稍候……")
            if run == "抓取":
                self._last_fetch_summary["status"] = "抓取中……"
                try:
                    self._render_fetch_status()
                except Exception:
                    pass
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

    def _count_failed(self, storage, source_id: str) -> int:
        """返回指定站点抓取失败（status='failed'）的文章数，用于导出日志。"""
        try:
            return storage.count_by_status(source_id=source_id).get("failed", 0)
        except Exception:
            return 0

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
                usable = sum(storage.count_usable(source_id=sid) for sid in site_ids)
                self._last_fetch_at = self._read_last_fetch_at(storage, site_ids)
                self._analysis_status = self._read_analysis_status(storage, site_ids)
        except Exception as exc:
            self.log(f"状态读取失败：{exc}")
            eco_count = 0
            hkej_count = 0
            usable = 0

        ai_ok, ai_failed = self._analysis_status
        current = self.site_var.get()
        values = {
            "db": f"数据库：{self.db_path}",
            "eco_count": f"ECO 新闻：{eco_count}",
            "hkej_count": f"HKEJ 新闻：{hkej_count}",
            "rfi_count": f"RFI 新闻：{rfi_count}",
            "usable": f"可读新闻：{usable}",
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
        # 更新顶部状态摘要的“目标”数量，使抓取中状态更完整
        self._last_fetch_summary["limit"] = limit
        self._last_fetch_summary["source"] = self._source_display(self._active_source)
        try:
            self._render_fetch_status()
        except Exception:
            pass

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
        # 主线程读取来源列表（worker 不触碰 tkinter 变量）
        site_ids = self._selected_site_ids()
        self._set_busy(True, run="AI 分析")

        def worker() -> None:
            try:
                self._run_ai_analyze(limit, site_ids=site_ids)
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
        site_ids = self._selected_site_ids()
        self._set_busy(True, run="打开新闻库")

        def worker() -> None:
            try:
                self._run_news_archive(limit, site_ids=site_ids)
            except Exception as exc:
                self._bg_log(f"新闻库导出/打开失败：\n{exc}")
            finally:
                self._queue.put("__ARCHIVE_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_research(self) -> None:
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        site_ids = self._selected_site_ids()
        self._set_busy(True, run="打开 AI 研究结果")

        def worker() -> None:
            try:
                self._run_research(site_ids=site_ids)
            except Exception as exc:
                self._bg_log(f"AI 研究结果导出/打开失败：\n{exc}")
            finally:
                self._queue.put("__RESEARCH_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _selected_export_mode(self) -> str:
        """返回当前导出方式内部 id（始终为 reader = 便携阅读包）。"""
        return _DEFAULT_EXPORT_MODE

    def _export_mode_label(self) -> str:
        return dict(_EXPORT_OPTIONS)[_DEFAULT_EXPORT_MODE]

    def _on_export(self) -> None:
        """【导出便携阅读包】统一入口 —— 一次生成 Portable HTML + Word（DOCX）。

        用户无需选择导出格式；内部只调用 portable_reader 导出器，它在同一目录
        同时产出 ``index.html`` 与 ``Laxinwen-<SITE>-<date>.docx``。

        在**主线程**读取来源列表（避免 worker 线程触碰 tkinter 变量），
        再交给后台线程执行导出。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        # 导出数量统一使用顶部 limit_var（顶部“最近 N 篇”是唯一权威 limit）。
        limit = self._parse_limit(self.limit_var.get(), what="导出")
        if limit is None:
            return
        # 主线程读取站点 id（worker 不触碰 tkinter 变量）
        site_ids = self._selected_site_ids()
        mode_label = self._export_mode_label()
        self._set_busy(True, run="导出")

        def worker() -> None:
            try:
                self._run_export_portable_reader(limit, site_ids=site_ids)
            except Exception as exc:
                self._bg_log(f"导出（{mode_label}）失败：\n{exc}")
            finally:
                self._queue.put("__PORTABLE_EXPORT_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _on_notion_sync(self) -> None:
        """后台扫描 Portable 包并同步到 Notion，不参与新闻抓取。"""
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        self._set_busy(True, run="Notion 同步")

        def worker() -> None:
            try:
                from .notion_sync import run_sync

                messages = run_sync()
                for message in messages:
                    self._bg_log(message)
            except Exception as exc:
                self._bg_log(f"Notion 同步失败：\n{exc}")
            finally:
                self._queue.put("__NOTION_SYNC_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_notion_status(self) -> None:
        """显示 Notion 配置和最近一次成功同步时间，不显示 Token。"""
        try:
            from .notion_sync import load_notion_config

            config = load_notion_config()
            configured = bool(config["token"] and config["root_page_id"])
            last_sync = None
            if config["state_path"].is_file():
                data = json.loads(config["state_path"].read_text(encoding="utf-8"))
                synced = [
                    item.get("synced_at")
                    for item in data.get("packages", {}).values()
                    if item.get("synced_at")
                ]
                last_sync = max(synced) if synced else None
            status = "已配置" if configured else "未配置"
            self.notion_status_var.set(
                f"状态：{status} · 最后同步：{last_sync or '—'}"
            )
        except Exception:
            self.notion_status_var.set("状态：未配置 · 最后同步：—")

    # ------------------------------------------------------------------ 定时抓取（多任务列表）

    # ---- 任务列表刷新与选择 ----

    def _apply_scheduler_to_ui(self) -> None:
        """把已保存的定时任务列表回显到 UI（任务表格）。

        启动时只读取状态、不自动创建任何 Windows 定时任务（见需求第十四条）：
        GUI 仅查询并显示最终状态，只有用户点击「安装/更新」才真正创建任务。
        """
        self._refresh_windows_task_state()
        self._refresh_scheduler_table()

    # ---- Windows Task Scheduler 状态查询（只读，不自动安装） ----

    def _query_windows_task(self, job: SchedulerConfig) -> dict:
        """查询单个 job 对应 Windows 定时任务的真实状态（只读，不创建）。

        复用 task_scheduler.query_task() 的 schtasks 命令；返回：

            {"exists": bool, "enabled": bool, "running": bool}

        查询失败（例如非 Windows / schtasks 不可用）时返回
        {"exists": False, "enabled": False, "running": False, "unknown": True}。

        不阻塞 GUI 主线程过久——schtasks /Query 为本地快速调用；
        任何异常都会被吞掉，绝不因查询失败而崩溃或自动安装任务。
        """
        try:
            result = self._scheduler_query(job)
        except Exception:
            # 非 Windows / 查询抛异常 → 视为状态未知
            return {"exists": False, "enabled": False, "running": False, "unknown": True}
        if not result or not result.get("ok"):
            # 任务不存在（schtasks 报“找不到”）→ 未安装；其它错误也保守视为不存在
            return {"exists": False, "enabled": False, "running": False, "unknown": False}
        if not result.get("executed"):
            # 未真正执行查询（如非 Windows 环境生成的预览命令）→ 状态未知
            return {"exists": False, "enabled": False, "running": False, "unknown": True}
        out = result.get("message", "") or ""
        return self._parse_task_query_output(out)

    @staticmethod
    def _parse_task_query_output(out: str) -> dict:
        """解析 schtasks /Query /V /FO LIST 输出，判断任务是否启用 / 正在运行。

        兼容中英文 Windows 输出，逐行匹配已知字段名与状态值；
        匹配不到相关状态时保守返回“未知”。
        """
        # schtasks /V /FO LIST 用多个空格对齐字段名与值（如
        # “Scheduled Task State:                    Enabled”），若直接按单个空格做
        # 子串匹配会漏判。因此先把所有连续空白折叠为单个空格再匹配，保证在真实
        # Windows 输出下也能正确识别“已启用 / 执行中”。
        norm = re.sub(r"\s+", " ", out).strip()
        norm_lower = norm.lower()
        enabled = False
        running = False
        # 任务已启用：英文 "Scheduled Task State: Enabled" / 中文 “计划任务状态: 已启用”
        if "task state: enabled" in norm_lower or "任务状态: 已启用" in norm or "计划任务状态: 已启用" in norm:
            enabled = True
        # 正在运行：英文 "Status: Running" / 中文 “状态: 正在运行”
        if "status: running" in norm_lower or "状态: 正在运行" in norm or "运行中" in norm:
            running = True
        # 若已明确读到 enabled 标志，则 exists=True
        exists = bool(norm_lower)
        return {"exists": exists, "enabled": enabled, "running": running, "unknown": False}

    def _refresh_windows_task_state(self) -> None:
        """对每个 job 重新查询 Windows 定时任务状态，更新缓存。

        只读：不会创建 / 修改任何 Windows 任务。
        """
        for job in self._scheduler_jobs:
            self._sched_win_state[job.job_id] = self._query_windows_task(job)

    def _get_scheduler_display_status(self, job: SchedulerConfig) -> str:
        """计算单个 job 的「最终用户可见状态」。

        内部状态（job.enabled + Windows 任务是否存在/启用/运行）对外只合并为一个
        用户能理解的状态：

            已启用   = 已启用 + Windows 任务存在 + Windows 任务启用
            未安装   = 已启用 + Windows 任务不存在
            已停用   = 用户主动停用（enabled=False）或 Windows 任务被禁用
            安装失败 = 最近一次「安装/更新」失败
            执行中   = Windows 任务正在运行
            状态未知 = 无法查询 Windows 任务

        不再向用户同时展示两套状态（已启用 + 已安装），只给一个最终状态。
        """
        # 安装失败优先显示（用户刚点了安装但没成功）
        if job.job_id in self._sched_install_failed:
            return "安装失败"
        if not job.enabled:
            return "已停用"
        state = self._sched_win_state.get(job.job_id)
        if state is None or state.get("unknown"):
            return "状态未知"
        if not state.get("exists"):
            return "未安装"
        if not state.get("enabled"):
            # Windows 任务存在但被禁用 → 已停用
            return "已停用"
        if state.get("running"):
            return "执行中"
        return "已启用"

    def _refresh_scheduler_table(self) -> None:
        """刷新任务列表 Treeview。"""
        tree = self.sched_tree
        for item in tree.get_children():
            tree.delete(item)
        for job in self._scheduler_jobs:
            freq = self._job_freq_text(job)
            tree.insert(
                "", "end",
                iid=job.job_id,
                values=(
                    job.display_name(),
                    job.source.upper(),
                    freq,
                    str(job.limit),
                    self._get_scheduler_display_status(job),
                ),
            )
        if not self._scheduler_jobs:
            self.sched_status_var.set("自动抓取：未配置任何定时任务")
            return
        self._refresh_sched_status()

    def _job_freq_text(self, job: SchedulerConfig) -> str:
        """返回任务的频率展示文本。"""
        if job.frequency == FREQ_HOURLY:
            return f"每小时 / {job.interval_hours} 小时"
        return f"每日 {job.time}"

    def _on_sched_select(self, _event=None) -> None:
        """任务列表选中项变化时记录当前选中 job。"""
        sel = self.sched_tree.selection()
        self._selected_job = None
        if sel:
            for job in self._scheduler_jobs:
                if job.job_id == sel[0]:
                    self._selected_job = job
                    break

    def _selected_job_or_warn(self) -> Optional[SchedulerConfig]:
        """返回当前选中的 job；未选中时提示并返回 None。"""
        if self._selected_job is None:
            self.log("请先在任务列表中选择一个定时任务。")
            return None
        return self._selected_job

    def _save_jobs(self) -> bool:
        """把当前任务列表持久化。失败返回 False。"""
        try:
            self._scheduler_save_jobs(self._scheduler_jobs, self._scheduler_config_path)
        except Exception as exc:
            self.log(f"保存定时任务配置失败：\n{exc}")
            return False
        return True

    def _refresh_sched_status(self) -> None:
        """刷新「自动抓取」底部汇总（使用最终状态，不用内部 enabled）。"""
        if not self._scheduler_jobs:
            self.sched_status_var.set("自动抓取：未配置任何定时任务")
            return
        counts: dict[str, int] = {}
        for job in self._scheduler_jobs:
            st = self._get_scheduler_display_status(job)
            counts[st] = counts.get(st, 0) + 1
        running = counts.get("已启用", 0)
        parts = [f"自动抓取：{running} 个任务已启用"]
        for st in ("未安装", "已停用", "安装失败", "执行中", "状态未知"):
            n = counts.get(st, 0)
            if n:
                parts.append(f"{n} 个{st}")
        self.sched_status_var.set("    ".join(parts) + "    自动导出：自动启用")

    # ---- 新建 / 编辑 / 启用停用 ----

    def _on_sched_new(self) -> None:
        """【新建任务】—— 打开任务编辑对话框。"""
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = _JobsDialog(self.root, title="新建定时任务", job=None)
        if job.result is None:
            return
        # job id 必须唯一
        for existing in self._scheduler_jobs:
            if existing.job_id == job.result.job_id:
                messagebox.showerror(
                    "任务 id 已存在",
                    f"任务 id「{job.result.job_id}」已存在，请使用其它名称。",
                    parent=self.root,
                )
                return
        self._scheduler_jobs.append(job.result)
        if not self._save_jobs():
            return
        self.log(f"已新建定时任务：{job.result.job_id}")
        # 新任务未安装到 Windows → 状态为「未安装」（enabled 时）
        self._refresh_windows_task_state()
        self._refresh_scheduler_table()

    def _on_sched_edit(self) -> None:
        """【编辑】—— 修改选中任务。"""
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = self._selected_job_or_warn()
        if job is None:
            return
        dlg = _JobsDialog(self.root, title="编辑定时任务", job=job)
        if dlg.result is None:
            return
        # 保留原 id（id 唯一稳定），仅更新其它字段
        old_id = job.job_id
        new_job = dlg.result
        new_job.id = old_id
        for i, existing in enumerate(self._scheduler_jobs):
            if existing.job_id == old_id:
                self._scheduler_jobs[i] = new_job
                break
        if not self._save_jobs():
            return
        self.log(f"已更新定时任务：{new_job.job_id}")
        self._refresh_windows_task_state()
        self._refresh_scheduler_table()

    def _on_sched_toggle(self) -> None:
        """【启用/停用】—— 真正同步 Laxinwen 配置与 Windows Task Scheduler。

        - 启用：enabled=true → 若 Windows 任务不存在则自动安装；若存在但被禁用则
          自动启用。只有 Laxinwen 与 Windows 两边都就绪才显示「已启用」。
        - 停用：enabled=false → 同步 Disabled Windows 任务。

        全程在后台线程执行，避免阻塞 GUI 主线程。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = self._selected_job_or_warn()
        if job is None:
            return
        ok, reason = job.is_valid()
        if not ok:
            self.log(f"定时任务参数无效：{reason}")
            return
        target_enabled = not job.enabled
        job.enabled = target_enabled
        if not self._save_jobs():
            job.enabled = not target_enabled  # 回滚
            return
        self._set_busy(True, run="启用" if target_enabled else "停用")
        self.log(
            f"正在{('启用' if target_enabled else '停用')}任务「{job.job_id}」并同步 Windows 计划任务……"
        )
        job_ref = job

        def worker() -> None:
            try:
                if target_enabled:
                    self._enable_job_sync(job_ref)
                else:
                    self._disable_job_sync(job_ref)
            except Exception as exc:
                self._bg_log(f"同步 Windows 计划任务失败：{exc}")
            finally:
                self._queue.put("__SCHED_TOGGLE_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    def _enable_job_sync(self, job: SchedulerConfig) -> None:
        """启用 job：确保 Windows 任务存在且启用（不存在→安装；禁用→启用）。"""
        self._bg_log(f"任务「{job.job_id}」已启用（scheduler.json）")
        # 1. 查询 Windows 任务当前状态
        state = self._query_windows_task(job)
        if not state.get("exists"):
            # 2a. 任务不存在 → 自动安装
            self._bg_log(f"Windows 计划任务不存在，自动安装……")
            result = self._scheduler_install(job)
            if not result.get("ok"):
                self._sched_install_failed.add(job.job_id)
                self._bg_log(f"安装失败：{job.job_id}")
                self._bg_log(f"原因：{result.get('message', '未知错误')}")
                return
            self._sched_install_failed.discard(job.job_id)
            self._bg_log(f"Windows 计划任务已安装：{result.get('task_name', job.task_name())}")
        elif not state.get("enabled"):
            # 2b. 任务存在但被禁用 → 自动启用
            self._bg_log(f"Windows 计划任务存在但被禁用，自动启用……")
            result = self._scheduler_enable(job)
            if not result.get("ok"):
                self._sched_install_failed.add(job.job_id)
                self._bg_log(f"启用失败：{job.job_id}")
                self._bg_log(f"原因：{result.get('message', '未知错误')}")
                return
            self._sched_install_failed.discard(job.job_id)
            self._bg_log(f"Windows 计划任务已启用：{result.get('task_name', job.task_name())}")
        # 3. 重新查询最终状态
        self._refresh_windows_task_state()
        final = self._get_scheduler_display_status(job)
        if final == "已启用":
            self._bg_log(f"任务「{job.job_id}」状态：已启用（Laxinwen + Windows 均已就绪）")
        elif final == "执行中":
            self._bg_log(f"任务「{job.job_id}」状态：已启用且正在执行")
        else:
            self._bg_log(f"任务「{job.job_id}」最终状态：{final}")

    def _disable_job_sync(self, job: SchedulerConfig) -> None:
        """停用 job：同步 Disabled Windows 任务（若存在）。"""
        self._bg_log(f"任务「{job.job_id}」已停用（scheduler.json）")
        state = self._query_windows_task(job)
        if state.get("exists"):
            if state.get("enabled"):
                self._bg_log(f"同步停用 Windows 计划任务……")
                result = self._scheduler_disable(job)
                if not result.get("ok"):
                    self._bg_log(f"停用 Windows 计划任务失败：{job.job_id}")
                    self._bg_log(f"原因：{result.get('message', '未知错误')}")
                else:
                    self._bg_log(f"Windows 计划任务已停用：{result.get('task_name', job.task_name())}")
            else:
                self._bg_log(f"Windows 计划任务已处于停用状态")
        else:
            self._bg_log(f"Windows 计划任务不存在（无需停用）")
        # 重新查询最终状态
        self._refresh_windows_task_state()
        final = self._get_scheduler_display_status(job)
        self._bg_log(f"任务「{job.job_id}」状态：{final}")

    # ---- 安装 / 更新（写入 Windows Task Scheduler） ----

    def _on_sched_install(self) -> None:
        """【安装/更新】—— 为选中任务创建/更新 Windows Task Scheduler 任务。

        停用的任务不安装（并尝试移除已存在的同名任务）。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = self._selected_job_or_warn()
        if job is None:
            return
        ok, reason = job.is_valid()
        if not ok:
            self.log(f"定时任务参数无效：{reason}")
            return
        if not self._save_jobs():
            return
        if not job.enabled:
            self.log(f"任务「{job.job_id}」为停用状态，将移除已存在的同名任务（如有）。")
            self._set_busy(True, run="停用自动抓取")

            def worker_disable() -> None:
                try:
                    result = self._scheduler_delete(job)
                    self._bg_log(
                        f"停用自动抓取：{result.get('task_name', '')} "
                        f"→ {'已移除' if result.get('ok') else '操作失败'}"
                    )
                    if result.get("message"):
                        self._bg_log(result["message"])
                except Exception as exc:
                    self._bg_log(f"停用自动抓取失败：\n{exc}")
                finally:
                    self._queue.put("__SCHED_DELETE_DONE__")

            threading.Thread(target=worker_disable, daemon=True).start()
            return
        self._set_busy(True, run="安装/更新定时任务")
        self.log(f"正在安装/更新任务：{job.job_id}")

        def worker() -> None:
            try:
                result = self._scheduler_install(job)
                if result.get("ok"):
                    self._sched_install_failed.discard(job.job_id)
                    self._bg_log(
                        f"Windows 定时任务已安装：{result.get('task_name', '')}"
                    )
                    if result.get("message"):
                        self._bg_log(result["message"])
                else:
                    self._sched_install_failed.add(job.job_id)
                    self._bg_log(f"安装失败：{job.job_id}")
                    self._bg_log(f"原因：{result.get('message', '未知错误')}")
            except Exception as exc:
                self._sched_install_failed.add(job.job_id)
                self._bg_log(f"安装失败：{job.job_id}")
                self._bg_log(f"原因：{exc}")
            finally:
                self._queue.put("__SCHED_INSTALL_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    # ---- 删除 ----

    def _on_sched_delete(self) -> None:
        """【删除】—— 删除选中任务及其 Windows Task Scheduler 任务。

        删除一个任务不影响其它任务。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = self._selected_job_or_warn()
        if job is None:
            return
        if not messagebox.askyesno(
            "删除定时任务",
            f"确定要删除定时任务「{job.job_id}」（{job.task_name()}）吗？\n\n"
            "删除后该任务不再自动抓取，其它任务不受影响。",
            parent=self.root,
        ):
            self.log("已取消删除定时任务。")
            return
        self._set_busy(True, run="删除定时任务")
        self.log(f"删除任务：{job.job_id}")
        job_ref = job

        def worker() -> None:
            try:
                result = self._scheduler_delete(job_ref)
                if result.get("ok"):
                    self._bg_log(
                        f"Windows 定时任务已删除：{result.get('task_name', '')}"
                    )
                    if result.get("message"):
                        self._bg_log(result["message"])
                else:
                    # Windows 删除失败：不要静默，记录原因（但仍删除配置）
                    self._bg_log(f"Windows 定时任务删除失败：{job_ref.job_id}")
                    self._bg_log(f"原因：{result.get('message', '未知错误')}")
                # 从任务列表移除并保存
                self._scheduler_jobs = [
                    j for j in self._scheduler_jobs if j.job_id != job_ref.job_id
                ]
                self._sched_win_state.pop(job_ref.job_id, None)
                self._sched_install_failed.discard(job_ref.job_id)
                self._selected_job = None
                self._save_jobs()
                self._bg_log(f"job 已从配置中删除：{job_ref.job_id}")
                self._refresh_scheduler_table()
            except Exception as exc:
                self._bg_log(f"删除定时任务失败：{job_ref.job_id}")
                self._bg_log(f"原因：{exc}")
            finally:
                self._queue.put("__SCHED_DELETE_DONE__")

        threading.Thread(target=worker, daemon=True).start()

    # ---- 立即运行一次（schtasks /Run，不阻塞 GUI 主线程） ----

    def _on_sched_run_now(self) -> None:
        """【立即运行一次】—— 调用 schtasks /Run 触发对应 Windows 任务。

        GUI 不得冻结：这里在后台线程执行 schtasks /Run，主线程保持响应。
        如果任务已停用，则不调用 schtasks /Run，直接提示用户先启用；
        如果任务尚未安装，则提示用户先安装。
        """
        if self._busy:
            self.log("已有任务运行中，请等待完成。")
            return
        job = self._selected_job_or_warn()
        if job is None:
            return
        ok, reason = job.is_valid()
        if not ok:
            self.log(f"定时任务参数无效：{reason}")
            return
        if not self._save_jobs():
            return
        # 停用任务不能立即运行：不调用 schtasks /Run，直接提示
        if not job.enabled:
            self.log(f"任务已停用，请先启用任务「{job.job_id}」。")
            return
        self._set_busy(True, run="立即运行一次")
        self.log(f"立即运行：{job.job_id}")

        def worker() -> None:
            try:
                # 先确认任务已安装，未安装则提示用户先安装，不发送 /Run
                st = self._query_windows_task(job)
                if not st.get("exists"):
                    self._bg_log(
                        f"无法立即运行：{job.job_id}\n"
                        "该任务尚未安装，请先点击「安装/更新」。"
                    )
                    return
                # 与 Windows 定时任务完全一致：通过 schtasks /Run 触发对应任务。
                result = self._scheduler_run_now(job)
                if result.get("ok"):
                    # schtasks /Run 成功仅表示 Windows 已接受执行请求，
                    # 真正抓取结果见 data/logs/scheduled-fetch.log
                    self._bg_log(
                        f"已请求 Windows Task Scheduler 执行任务：{job.job_id}\n"
                        "后台抓取正在运行，结果请查看 scheduled-fetch.log"
                    )
                else:
                    self._bg_log(f"立即运行失败：{job.job_id}")
                    self._bg_log(f"原因：{result.get('message', '未知错误')}")
            except Exception as exc:
                self._bg_log(f"立即运行失败：{job.job_id}")
                self._bg_log(f"原因：{exc}")
            finally:
                self._queue.put("__SCHED_RUNNOW_DONE__")

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
        """后台执行手动抓取：复用现有 pipeline，实时输出进度到 GUI 日志。

        抓取完成后固定自动导出便携阅读包（与定时任务一致），日志明确区分
        FETCH 与 EXPORT 结果，并更新顶部抓取状态摘要。
        """
        site_ids = self._site_ids_for(self._active_source)
        source_display = self._source_display(self._active_source)
        self._bg_log(f"开始抓取 {source_display} 最新 {limit} 篇")
        # 打开新连接（线程内使用）；多个站点共用一个 Storage 连接
        totals = {
            "discovered": 0, "duplicated": 0, "fetched_ok": 0,
            "extracted_ok": 0, "low_quality": 0, "failed": 0, "usable": 0,
        }
        with self._storage_factory(self.db_path) as storage:
            pipeline = self._pipeline_factory(storage, limit)
            try:
                for sid in site_ids:
                    stats = pipeline.run_site(sid)
                    s = stats
                    for k in totals:
                        totals[k] += getattr(s, k, 0)
                    self._bg_log(
                        f"{self._sep}\n"
                        f"[{self._source_display(sid)}] 发现：{s.discovered}\n"
                        f"重复：{s.skipped_dup}\n"
                        f"正文下载成功：{s.fetched_ok}\n"
                        f"正文提取成功：{s.extracted_ok}\n"
                        f"质量不合格：{s.low_quality}\n"
                        f"抓取/提取失败：{s.failed}\n"
                        f"可读新闻：{s.usable} / 目标 {limit}"
                    )
                    if s.usable < limit and s.discovered > 0:
                        # 候选耗尽但 usable < limit：明确报告“候选不足”，不伪装成抓取失败
                        self._bg_log(
                            f"[{self._source_display(sid)}] 候选已耗尽，可读新闻 {s.usable} / 目标 {limit}"
                        )
                    if s.usable == 0 and s.discovered == 0:
                        self._bg_log(
                            f"[{self._source_display(sid)}] 没有发现新的可读新闻（发现 0 条）"
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

            # 抓取阶段结果（候选耗尽 / 没有新文章都不算失败，只有异常才算）
            self._bg_log(
                f"{self._sep}\n"
                f"合计发现 {totals['discovered']} 篇 · 重复 {totals['duplicated']} · "
                f"可读 {totals['usable']} / 目标 {limit} · 失败 {totals['failed']}"
            )
            if totals["usable"] == 0 and totals["discovered"] == 0:
                self._bg_log("没有发现新的可读新闻")
            self._bg_log("FETCH: SUCCESS")

            # 固定自动导出（与定时任务一致：便携阅读包）
            export_status = self._auto_export_after_fetch(
                storage, source_display, limit, totals
            )

        self._bg_log(f"抓取完成（{source_display}，limit={limit}）")
        # 更新顶部抓取状态摘要（worker 线程只更新 dict，主线程渲染）
        self._last_fetch_summary = {
            "source": source_display,
            "limit": limit,
            "discovered": totals["discovered"],
            "duplicated": totals["duplicated"],
            "usable": totals["usable"],
            "failed": totals["failed"],
            "export": export_status,
            "status": "已完成",
        }

    def _auto_export_after_fetch(self, storage, source_display: str, limit: int,
                                 totals: dict) -> str:
        """抓取完成后固定自动导出便携阅读包（与定时任务一致）。

        返回导出状态字符串：成功 / 失败 / 跳过。导出失败不影响抓取结果。
        """
        try:
            self._bg_log("自动导出开始（便携阅读包）……")
            out_dir = (
                self.portable_dir
                / f"Laxinwen-{source_display}-{datetime.now().strftime('%Y-%m-%d')}"
            )
            self._bg_log(f"正在导出便携阅读包（最近 {limit} 篇）→ {out_dir}")
            result = self._portable_reader_export(
                storage,
                out_dir,
                source_id=self._active_source if self._active_source in ("rfi", "eco", "hkej") else "rfi",
                limit=limit,
                research_root=self.research_dir,
            )
            bat = out_dir / "Open-Reader.bat"
            if not (out_dir / "index.html").exists() or not bat.exists():
                raise FileNotFoundError(f"便携阅读包未生成：{out_dir}")
            self._bg_log(f"便携阅读包导出完成：{result.exported} 篇 → {out_dir}")
            self._bg_log("EXPORT: SUCCESS")
            return "成功"
        except Exception as exc:
            self._bg_log(f"自动导出失败：{exc}")
            self._bg_log("EXPORT: FAILED")
            return "失败"

    def _run_ai_analyze(self, limit: int, *, site_ids=None) -> None:
        if site_ids is None:
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

    def _run_news_archive(self, limit: int, *, site_ids=None) -> None:
        if site_ids is None:
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
                    f"{self._source_display(sid)} News Archive 导出完成：{result.exported} 篇\n"
                    f"数据库历史抓取失败记录（未导出）：{self._count_failed(storage, sid)}\n"
                    f"AI 已分析：{result.analyzed_ok}\n"
                    f"AI 分析失败：{result.analyzed_failed}\n"
                    f"AI 未分析：{result.unanalyzed}"
                )
                # 本地 HTTP 阅读模式：打开 http://127.0.0.1:<port>/news-html/<site>/index.html
                url = self._http_url_for(f"news-html/{sid}/index.html")
                self._bg_log(f"{self._source_display(sid)} 新闻库已启动：\n{url}")
                self._open_url(url)

    def _run_research(self, *, site_ids=None) -> None:
        if site_ids is None:
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
                    f"{self._source_display(sid)} 独立 HTML 导出完成：{result.exported} 篇\n"
                    f"数据库历史抓取失败记录（未导出）：{self._count_failed(storage, sid)}\n"
                    f"AI 已分析：{result.analyzed_ok}\n"
                    f"AI 分析失败：{result.analyzed_failed}\n"
                    f"AI 未分析：{result.unanalyzed}"
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
                    f"{self._source_display(sid)} HTML 新闻包导出完成：{result.exported} 篇\n"
                    f"数据库历史抓取失败记录（未导出）：{self._count_failed(storage, sid)}\n"
                    f"AI 已分析：{result.analyzed_ok}\n"
                    f"AI 分析失败：{result.analyzed_failed}\n"
                    f"AI 未分析：{result.unanalyzed}"
                )
                self._bg_log(f"📚 HTML 新闻包目录：\n{out_dir}")
                self._bg_log("可复制整个目录到其它电脑，双击 index.html 阅读。")

    def _run_export_portable_reader(self, limit: int, *, site_ids=None) -> None:
        """导出便携阅读包（HTML + Word 一次生成；供他人使用）。

        便携阅读包 = Portable HTML + Word（DOCX）一次生成。HTML 供浏览器阅读，
        Word 供研究阅读；二者同批、同一目录。

        ``site_ids``（可选）由主线程传入，避免 worker 线程读取 tkinter 变量。
        """
        if site_ids is None:
            site_ids = self._selected_site_ids()
        research_root = self.research_dir
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_dir = self.portable_dir / f"Laxinwen-{sid.upper()}-{datetime.now().strftime('%Y-%m-%d')}"
                self._bg_log(
                    f"正在导出 {self._source_display(sid)} 便携阅读包（HTML + Word，最近 {limit} 篇）→ {out_dir}"
                )
                result = self._portable_reader_export(
                    storage, out_dir, source_id=sid, limit=limit,
                    research_root=research_root,
                )
                bat = out_dir / "Open-Reader.bat"
                docx = out_dir / f"{out_dir.name}.docx"
                if not (out_dir / "index.html").exists() or not bat.exists():
                    raise FileNotFoundError(f"便携阅读包（HTML）未生成：{out_dir}")
                word_status = (
                    f"Word 研究阅读包：\n{docx}" if docx.exists()
                    else f"⚠ Word 研究阅读包未生成：{docx}"
                )
                self._bg_log(
                    f"{self._source_display(sid)} 便携阅读包导出完成：{result.exported} 篇\n"
                    f"数据库历史抓取失败记录（未导出）：{self._count_failed(storage, sid)}\n"
                    f"AI 已分析：{result.analyzed_ok}\n"
                    f"AI 分析失败：{result.analyzed_failed}\n"
                    f"AI 未分析：{result.unanalyzed}"
                )
                self._bg_log(f"📦 便携阅读包目录：\n{out_dir}")
                self._bg_log(word_status)
                self._bg_log(
                    "HTML：双击 Open-Reader.bat 在浏览器阅读（沉浸式翻译等扩展可正常工作）；"
                    "Word：双击 .docx 研究阅读，目录条目可点击跳转，正文可点击「返回目录」回到目录。"
                )

    def _run_export_word(self, limit: int) -> None:
        """导出 Word（DOCX）研究阅读包（目录可点击跳转 + 原文 URL 超链接）。"""
        site_ids = self._selected_site_ids()
        word_dir = self.export_root / "word"
        word_dir.mkdir(parents=True, exist_ok=True)
        with self._storage_factory(self.db_path) as storage:
            for sid in site_ids:
                out_path = word_dir / f"Laxinwen-{sid.upper()}-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.docx"
                self._bg_log(
                    f"正在导出 {self._source_display(sid)} Word 研究阅读包（最近 {limit} 篇）→ {out_path}"
                )
                result = self._word_export(
                    storage, out_path, source_id=sid, limit=limit
                )
                if not out_path.exists():
                    raise FileNotFoundError(f"Word 阅读包未生成：{out_path}")
                self._bg_log(
                    f"{self._source_display(sid)} Word 研究阅读包导出完成：{result.exported} 篇\n"
                    f"AI 已分析：{result.analyzed_ok}\n"
                    f"AI 分析失败：{result.analyzed_failed}\n"
                    f"AI 未分析：{result.unanalyzed}"
                )
                self._bg_log(f"📝 Word 研究阅读包：\n{out_path}")
                self._bg_log(
                    "目录条目可点击跳转到对应新闻；原文链接可在 Word 中点击打开浏览器。"
                )


# ---------- 定时任务新建/编辑对话框 ----------


class _JobsDialog:
    """新建 / 编辑单个定时任务的对话框（Toplevel）。

    返回：``dialog.result`` 为一个 ``SchedulerConfig``（自动导出固定启用，
    不再让普通用户选择）。取消时 ``result`` 为 None。
    """

    def __init__(self, parent, *, title: str, job: Optional[SchedulerConfig] = None):
        self.result: Optional[SchedulerConfig] = None
        self._parent = parent
        job = job or SchedulerConfig()
        self._job = job

        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.resizable(False, False)

        body = ttk.Frame(self.top, padding=12)
        body.pack(fill="both", expand=True)

        # 名称 / id
        ttk.Label(body, text="任务名称：").grid(row=0, column=0, sticky="e", pady=3)
        self.name_var = tk.StringVar(value=job.name)
        ttk.Entry(body, textvariable=self.name_var, width=28).grid(
            row=0, column=1, sticky="w", padx=(6, 0), pady=3
        )

        ttk.Label(body, text="任务 id（唯一）：").grid(row=1, column=0, sticky="e", pady=3)
        self.id_var = tk.StringVar(value=job.id or "")
        id_entry = ttk.Entry(body, textvariable=self.id_var, width=28)
        id_entry.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=3)
        if job.id:
            id_entry.configure(state="readonly")  # 编辑时 id 不可改
        ttk.Label(
            body,
            text="如 rfi-hourly（小写字母/数字/短横线，用于 Windows 任务与日志）",
            foreground="#888",
        ).grid(row=2, column=1, sticky="w", padx=(6, 0))

        # 来源
        ttk.Label(body, text="新闻来源：").grid(row=3, column=0, sticky="e", pady=3)
        self.source_var = tk.StringVar(value=job.source if job.source in ("rfi", "eco", "hkej") else "rfi")
        ttk.Combobox(
            body, textvariable=self.source_var, state="readonly", width=10, values=["rfi", "eco", "hkej"]
        ).grid(row=3, column=1, sticky="w", padx=(6, 0), pady=3)

        # 频率
        ttk.Label(body, text="频率：").grid(row=4, column=0, sticky="e", pady=3)
        self.freq_var = tk.StringVar(value=job.frequency if job.frequency in (FREQ_DAILY, FREQ_HOURLY) else FREQ_DAILY)
        ttk.Combobox(
            body, textvariable=self.freq_var, state="readonly", width=8, values=[FREQ_DAILY, FREQ_HOURLY]
        ).grid(row=4, column=1, sticky="w", padx=(6, 0), pady=3)
        self.freq_var.trace_add("write", lambda *a: self._sync_freq_fields())

        # 每日时间 / 每小时间隔
        ttk.Label(body, text="每日时间：").grid(row=5, column=0, sticky="e", pady=3)
        self.time_var = tk.StringVar(value=job.time or "08:00")
        self.time_entry = ttk.Entry(body, textvariable=self.time_var, width=8)
        self.time_entry.grid(row=5, column=1, sticky="w", padx=(6, 0), pady=3)

        ttk.Label(body, text="每小时间隔：").grid(row=6, column=0, sticky="e", pady=3)
        interval = job.interval_hours if job.interval_hours in HOURLY_INTERVALS else 1
        self.interval_var = tk.StringVar(value=f"{interval} 小时")
        self.interval_combo = ttk.Combobox(
            body,
            textvariable=self.interval_var,
            state="readonly",
            width=8,
            values=[f"{h} 小时" for h in HOURLY_INTERVALS],
        )
        self.interval_combo.grid(row=6, column=1, sticky="w", padx=(6, 0), pady=3)

        # 每次抓取数量
        ttk.Label(body, text="每次抓取数量：").grid(row=7, column=0, sticky="e", pady=3)
        self.limit_var = tk.StringVar(value=str(job.limit))
        ttk.Entry(body, textvariable=self.limit_var, width=8).grid(
            row=7, column=1, sticky="w", padx=(6, 0), pady=3
        )

        # 自动导出：固定启用（无需用户选择格式，内部统一 HTML + Word）
        ttk.Label(
            body, text="自动导出：", foreground="#666"
        ).grid(row=8, column=0, sticky="e", pady=3)
        ttk.Label(
            body,
            text="☑ 自动生成便携阅读包（HTML + Word）",
            foreground="#333",
        ).grid(row=8, column=1, sticky="w", padx=(6, 0), pady=3)
        ttk.Label(
            body, text="任务完成后自动生成 HTML 阅读包 + Word 研究阅读包", foreground="#888"
        ).grid(row=9, column=1, sticky="w", padx=(6, 0))

        # 按钮
        btns = ttk.Frame(body)
        btns.grid(row=10, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="确定", command=self._ok, style="Accent.TButton").pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="left")

        self._sync_freq_fields()
        self.top.bind("<Return>", lambda e: self._ok())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.wait_window()

    def _sync_freq_fields(self) -> None:
        """根据频率显示/隐藏 每日时间 或 每小时间隔。"""
        daily = self.freq_var.get() == FREQ_DAILY
        if daily:
            self.time_entry.grid()
            self.interval_combo.grid_remove()
        else:
            self.time_entry.grid_remove()
            self.interval_combo.grid()

    def _collect(self) -> Optional[SchedulerConfig]:
        cfg = SchedulerConfig()
        cfg.name = self.name_var.get().strip()
        cfg.id = self.id_var.get().strip()
        cfg.source = self.source_var.get()
        cfg.frequency = self.freq_var.get()
        cfg.time = self.time_var.get().strip() or "08:00"
        try:
            interval = int(str(self.interval_var.get()).replace("小时", "").strip())
        except ValueError:
            interval = 1
        cfg.interval_hours = interval if interval in HOURLY_INTERVALS else 1
        try:
            cfg.limit = int(self.limit_var.get().strip())
        except ValueError:
            cfg.limit = 50
        if cfg.limit <= 0:
            cfg.limit = 50
        # 自动导出固定启用（产品原则：定时任务的最终交付物是便携阅读包 = HTML + Word）
        cfg.auto_export = True
        # 内部保留 export_type 字段以兼容旧配置，但运行时统一解释为 HTML + DOCX。
        # 用户无需选择格式，因此这里不再从 GUI 下拉读取。
        cfg.export_type = EXPORT_PORTABLE
        return cfg

    def _ok(self) -> None:
        cfg = self._collect()
        ok, reason = cfg.is_valid()
        if not ok:
            messagebox.showerror("参数无效", reason, parent=self.top)
            return
        if not cfg.id:
            messagebox.showerror("缺少任务 id", "请填写任务 id（唯一标识，如 rfi-hourly）。", parent=self.top)
            return
        self.result = cfg
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()


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


def _default_word_export(storage, out_path, *, source_id, limit, job_id=""):
    from .word_export import export_word_package

    return export_word_package(
        storage, out_path, source_id=source_id, limit=limit, job_id=job_id
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
