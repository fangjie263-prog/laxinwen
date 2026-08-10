"""AI 设置对话框 —— 多 Provider 配置管理器（tkinter 独立设置窗口）。

对应需求「五、AI 设置窗口」与「核心产品目标：多个 AI Provider」：

- **Provider 列表**：下拉展示「预设 Provider」（OpenAI / Gemini / TokenRhythm）+
  已保存的自定义 Provider；
- **当前 Provider**：显示当前 Active Provider；
- **[＋ 新增 Provider]**：清空表单新建自定义 Provider；
- **[🗑 删除 Provider]**：确认后删除当前 Provider（只删该 Provider，不影响其它）；
- **Provider 名称**：可编辑输入框，允许任意自定义 Provider 名（不限定 preset）；
- **API Base URL / API Key / Model**：独立输入；
- **[刷新模型]**：调用 ``GET {base_url}/models`` 动态发现模型候选填入下拉，
  失败则提示“请手工输入 Model”（不阻断使用）；
- **[测试连接]**：真正发送一次极短请求；**测试成功则自动保存**并立即回调主 GUI 刷新
  （无需再点「保存」）；测试失败**不覆盖**已有配置；
- **[保存]**：手动保存当前表单（可跳过测试）。

安全约束：
- 任何日志 / 对话框文本 / 返回值都**不包含完整 API Key**（只显示掩码）；
- 保存走 ``config_store`` 写回项目根 .env（保留 CNB_TOKEN 等其它配置）；
- 「测试连接」复用 ``provider.test_connection``，错误映射成用户可读文案。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from .ai import config_store
from .ai.config_store import (
    AiConfig,
    ProviderConfig,
    masked,
    read_config,
    preset_base_url,
    preset_model_candidates,
)
from .ai.provider import AIProviderConfig, test_connection

logger = logging.getLogger(__name__)

# 预设 Provider（下拉里始终出现）
_PRESET_NAMES = ("openai", "gemini", "tokenrhythm")


class AiSettingsDialog:
    """AI 设置对话框（多 Provider 管理器）。构造时立即显示。"""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        current: Optional[AiConfig] = None,
        test_fn: Optional[Callable[[AIProviderConfig], object]] = None,
        on_save: Optional[Callable[[AiConfig], None]] = None,
        model_fetch_fn: Optional[Callable[[AIProviderConfig], list[str]]] = None,
    ):
        self._parent = parent
        self._test_fn = test_fn or self._default_test
        self._model_fetch_fn = model_fetch_fn or self._default_model_fetch
        self._on_save = on_save
        self._test_in_progress = False
        self._fetch_in_progress = False

        # 当前会话内已加载的 Provider 列表（含预设）
        self._providers = self._load_providers()
        # 当前表单对应哪个已保存的 Provider（None = 新建/未命名）
        self._editing_name: Optional[str] = None
        # 表单是否相对已保存状态发生修改（用于切换时提示）
        self._dirty = False

        cfg = current or read_config()

        self.win = tk.Toplevel(parent)
        self.win.title("AI Provider 管理器")
        self.win.geometry("620x600")
        self.win.minsize(560, 540)
        self.win.transient(parent)
        self.win.resizable(True, True)

        self._build_ui(cfg)
        self._refresh_provider_combo(select=cfg.provider or None)
        self._load_form_from(cfg)
        self._update_status()

    # ------------------------------------------------------------------ 数据

    def _load_providers(self) -> list[ProviderConfig]:
        """加载预设 + 已保存的自定义 Provider（去重，按名称）。"""
        saved = config_store.list_providers()
        by_name: dict[str, ProviderConfig] = {}
        for p in saved:
            if p.name:
                by_name[p.name] = p
        # 预设：确保 preset 出现在列表中（用已保存的配置，否则空）
        for preset in _PRESET_NAMES:
            if preset not in by_name:
                by_name[preset] = ProviderConfig(
                    name=preset,
                    base_url=preset_base_url(preset),
                )
        return list(by_name.values())

    def _provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    # ------------------------------------------------------------------ UI

    def _build_ui(self, cfg: AiConfig) -> None:
        pad = {"padx": 12, "pady": 4}
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- 当前 Provider + Provider 列表 ----
        top_row = ttk.Frame(outer)
        top_row.pack(fill="x", **pad)
        ttk.Label(top_row, text="Provider 配置列表：").pack(side="left")
        self.provider_combo = ttk.Combobox(top_row, width=24, state="readonly")
        self.provider_combo.pack(side="left", padx=(8, 0))
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_selected)

        add_btn = ttk.Button(top_row, text="＋ 新增", width=7, command=self._on_add_provider)
        add_btn.pack(side="left", padx=(8, 0))
        del_btn = ttk.Button(top_row, text="🗑 删除", width=7, command=self._on_delete_provider)
        del_btn.pack(side="left", padx=(6, 0))

        # ---- Provider 名称 ----
        ttk.Label(outer, text="Provider 名称：").pack(anchor="w", **pad)
        self.provider_name_var = tk.StringVar(value=cfg.provider or "")
        ttk.Entry(outer, textvariable=self.provider_name_var, width=50).pack(
            fill="x", padx=12
        )
        ttk.Label(
            outer,
            text="任意 OpenAI-compatible Provider 均可，不限于预设列表。",
            foreground="#888",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        # ---- API Base URL ----
        ttk.Label(outer, text="API Base URL：").pack(anchor="w", **pad)
        self.base_url_var = tk.StringVar(value=cfg.base_url or "")
        ttk.Entry(outer, textvariable=self.base_url_var, width=50).pack(fill="x", padx=12)

        # ---- API Key ----
        ttk.Label(outer, text="API Key：").pack(anchor="w", **pad)
        key_row = ttk.Frame(outer)
        key_row.pack(fill="x", padx=12)
        self.api_key_var = tk.StringVar(value=cfg.api_key or "")
        self.api_key_entry = ttk.Entry(
            key_row, textvariable=self.api_key_var, show="*", width=50
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            key_row, text="显示 Key", variable=self.show_key_var,
            command=self._on_toggle_key,
        ).pack(side="left", padx=(8, 0))

        # ---- Model ----
        ttk.Label(outer, text="Model：").pack(anchor="w", **pad)
        model_row = ttk.Frame(outer)
        model_row.pack(fill="x", padx=12)
        self.model_var = tk.StringVar(value=cfg.model or "")
        self.model_combo = ttk.Combobox(
            model_row, textvariable=self.model_var, width=40
        )
        self.model_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(model_row, text="刷新模型", command=self._on_refresh_models).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            outer, text="Model 支持下拉选择 + 手工输入（模型以 Provider /models 返回为准）。",
            foreground="#888",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        # ---- 按钮行 ----
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", padx=12, pady=(14, 4))
        self.test_btn = ttk.Button(
            btn_row, text="测试连接", command=self._on_test_connection
        )
        self.test_btn.pack(side="left")
        self.save_btn = ttk.Button(
            btn_row, text="保存", style="Accent.TButton", command=self._on_save_clicked
        )
        self.save_btn.pack(side="left", padx=(10, 0))
        ttk.Button(btn_row, text="取消", command=self.win.destroy).pack(
            side="left", padx=(10, 0)
        )

        # ---- 状态区 ----
        self.status_card = ttk.LabelFrame(outer, text="状态", padding=8)
        self.status_card.pack(fill="x", padx=12, pady=(10, 0))
        self.status_var = tk.StringVar(value="○ 尚未测试")
        ttk.Label(self.status_card, textvariable=self.status_var, justify="left", wraplength=520).pack(
            anchor="w"
        )

        # 追踪表单修改
        for var in (
            self.provider_name_var,
            self.base_url_var,
            self.api_key_var,
            self.model_var,
        ):
            var.trace_add("write", lambda *_: self._mark_dirty())

    # ------------------------------------------------------------------ 行为

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _on_toggle_key(self) -> None:
        if self.show_key_var.get():
            self.api_key_entry.configure(show="")
        else:
            self.api_key_entry.configure(show="*")

    def _current_provider(self) -> ProviderConfig:
        return ProviderConfig(
            name=self.provider_name_var.get().strip(),
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )

    def _current_ai_config(self) -> AIProviderConfig:
        """把当前表单组装成 AIProviderConfig（用于测试连接 / 刷新模型）。"""
        return AIProviderConfig(
            provider=self.provider_name_var.get().strip() or "openai-compatible",
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )

    def _default_test(self, config: AIProviderConfig):
        return test_connection(config)

    def _default_model_fetch(self, config: AIProviderConfig) -> list[str]:
        from .ai.provider import fetch_models

        return fetch_models(config)

    def _refresh_provider_combo(self, select: Optional[str] = None) -> None:
        names = self._provider_names()
        self.provider_combo.configure(values=names)
        if select and select in names:
            self.provider_combo.set(select)
        elif names:
            self.provider_combo.set(names[0])

    def _load_form_from(self, cfg) -> None:
        """把配置载入表单，并同步 model 下拉候选。"""
        self.provider_name_var.set(cfg.provider or "")
        self.base_url_var.set(cfg.base_url or "")
        self.api_key_var.set(cfg.api_key or "")
        self.model_var.set(cfg.model or "")
        # 模型候选：预设给默认候选；否则只保留当前值
        cands = preset_model_candidates(cfg.provider)
        if cfg.model and cfg.model not in cands:
            cands.insert(0, cfg.model)
        self.model_combo.configure(values=cands)
        self._editing_name = cfg.provider or None
        self._dirty = False

    def _load_provider_by_name(self, name: str) -> None:
        """按名称加载 Provider 到表单。预设自动填 Base URL + 模型候选；自定义恢复保存配置。"""
        p = next((p for p in self._providers if p.name == name), None)
        if p is None:
            # 预设但未保存过：用预设 Base URL
            p = ProviderConfig(name=name, base_url=preset_base_url(name))
        cfg = AiConfig(
            provider=p.name,
            base_url=p.base_url,
            api_key=p.api_key,
            model=p.model,
        )
        self._load_form_from(cfg)

    def _on_provider_selected(self, event=None) -> None:
        selected = self.provider_combo.get()
        if not selected:
            return
        if self._dirty and not self._confirm_discard():
            # 撤销选择
            if self._editing_name and self._editing_name in self._provider_names():
                self.provider_combo.set(self._editing_name)
            return
        self._load_provider_by_name(selected)
        self._update_status()

    def _on_add_provider(self) -> None:
        """新增自定义 Provider：清空表单，进入新建态。"""
        if self._dirty and not self._confirm_discard():
            return
        self._editing_name = None
        self.provider_name_var.set("")
        self.base_url_var.set("")
        self.api_key_var.set("")
        self.model_var.set("")
        self.model_combo.configure(values=[])
        self.provider_combo.set("")
        self._dirty = False
        self.status_var.set("○ 新建 Provider，请填写配置后点击「测试连接」。")

    def _on_delete_provider(self) -> None:
        name = self.provider_name_var.get().strip()
        if not name:
            messagebox.showinfo("删除 Provider", "当前没有可删除的 Provider。", parent=self.win)
            return
        if not messagebox.askyesno(
            "删除 Provider", f"确定删除 Provider “{name}” 吗？", parent=self.win
        ):
            return
        try:
            deleted = config_store.delete_provider(name)
        except Exception as exc:
            messagebox.showerror("删除失败", f"删除 Provider 失败：\n{exc}", parent=self.win)
            return
        self._providers = self._load_providers()
        self._refresh_provider_combo()
        if deleted:
            # 重新加载 active
            active = config_store.get_active_provider()
            self._load_form_from(active.to_ai_config())
            self.status_var.set(f"🗑 已删除 Provider：{name}\n当前 Active Provider：{active.name or '（无）'}")
            if self._on_save:
                # 通知主 GUI 刷新状态（配置可能已变化）
                self._on_save(active.to_ai_config())
        else:
            messagebox.showinfo("删除 Provider", f"未找到 Provider：{name}", parent=self.win)

    def _confirm_discard(self) -> bool:
        return messagebox.askyesno(
            "当前配置尚未保存", "当前配置尚未保存，是否放弃修改？", parent=self.win
        )

    def _on_refresh_models(self) -> None:
        if self._fetch_in_progress:
            return
        cfg = self._current_ai_config()
        if not cfg.base_url or not cfg.api_key:
            self._set_status("❌ 请先填写 API Base URL 和 API Key 再刷新模型。", kind=False)
            return
        self._fetch_in_progress = True
        self.status_var.set("⏳ 正在获取模型列表……")
        self._dirty = True

        def worker() -> None:
            try:
                models = self._model_fetch_fn(cfg)
            except Exception as exc:
                self.win.after(0, lambda: self._on_models_fetch_done(None, str(exc)))
                return
            self.win.after(0, lambda: self._on_models_fetch_done(models, None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_models_fetch_done(self, models, err: Optional[str]) -> None:
        self._fetch_in_progress = False
        if err:
            self._set_status(
                f"⚠️ 无法获取模型列表，请手工输入 Model。\n\n{err[:300]}", kind=False
            )
            return
        if not models:
            self._set_status("⚠️ 该 Provider 未返回模型列表，请手工输入 Model。", kind=False)
            return
        current = self.model_var.get().strip()
        candidates = list(models)
        if current and current not in candidates:
            candidates.insert(0, current)
        self.model_combo.configure(values=candidates)
        if not current:
            self.model_var.set(candidates[0])
        self._set_status(
            f"✅ 已获取 {len(models)} 个模型，可在 Model 下拉选择（也可手工输入）。",
            kind=True,
        )

    def _on_test_connection(self) -> None:
        if self._test_in_progress:
            return
        cfg = self._current_ai_config()
        if not cfg.base_url or not cfg.api_key or not cfg.model:
            self._set_status(
                "❌ 请先填写 API Base URL、API Key 和 Model 再测试。", kind=False
            )
            return
        if not cfg.provider and not cfg.base_url:
            self._set_status("❌ 请先填写 Provider 名称。", kind=False)
            return
        self._test_in_progress = True
        self.test_btn.configure(state="disabled")
        self.status_var.set("⏳ 正在测试连接（发送极短请求），请稍候……")

        def worker() -> None:
            try:
                result = self._test_fn(cfg)
            except Exception as exc:
                self.win.after(0, lambda: self._on_test_done(None, str(exc)))
                return
            self.win.after(0, lambda: self._on_test_done(result, None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, result, err: Optional[str]) -> None:
        self._test_in_progress = False
        self.test_btn.configure(state="normal")
        if err:
            self._set_status(f"❌ 测试失败\n\n{err[:400]}", kind=False)
            return
        ok = bool(getattr(result, "ok", False))
        msg = getattr(result, "message", "") or ("✅ 测试成功" if ok else "❌ 测试失败")
        self._set_status(msg, kind=ok)
        if ok:
            # 【测试成功 → 自动保存】立即保存并回调主 GUI 刷新，无需再点「保存」。
            self._auto_save_after_test()

    def _auto_save_after_test(self) -> None:
        """测试成功后自动保存当前配置，并立即回调主 GUI 刷新。"""
        p = self._current_provider()
        if not p.name:
            self._set_status("❌ 测试成功，但 Provider 名称不能为空，请填写。", kind=False)
            return
        cfg = p.to_ai_config()
        try:
            config_store.save_config(cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", f"保存 AI 配置失败：\n{exc}", parent=self.win)
            return
        # 更新会话内 Provider 列表（新增/更新）
        self._providers = self._load_providers()
        self._refresh_provider_combo(select=p.name)
        self._editing_name = p.name
        self._dirty = False
        if self._on_save:
            self._on_save(cfg)
        logger.info("测试成功，AI 配置已自动保存（API Key 已掩码，不打印）")
        self._set_status(
            f"{self.status_var.get()}\n\n🟢 已自动保存并立即生效（Active Provider）。",
            kind=True,
        )

    def _on_save_clicked(self) -> None:
        """手动保存当前配置（可跳过测试）。"""
        from .ai.config_store import save_config

        cfg = self._current_provider().to_ai_config()
        if not cfg.provider:
            messagebox.showwarning("保存", "请先填写 Provider 名称。", parent=self.win)
            return
        if not cfg.api_key:
            if not messagebox.askyesno(
                "保存", "API Key 为空。\n\n确定保存吗？（将保留 .env 中已有的 Key）",
                parent=self.win,
            ):
                return
        try:
            path = save_config(cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", f"保存 AI 配置失败：\n{exc}", parent=self.win)
            return
        self._providers = self._load_providers()
        self._refresh_provider_combo(select=cfg.provider)
        self._editing_name = cfg.provider
        self._dirty = False
        if self._on_save:
            self._on_save(cfg)
        logger.info("AI 配置已保存（API Key 已掩码，不打印）")
        messagebox.showinfo(
            "已保存",
            "AI 配置已保存到：\n"
            f"{path}\n\n"
            "已立即生效，可直接点击“🤖 AI 分析”。",
            parent=self.win,
        )
        self.win.destroy()

    def _set_status(self, text: str, *, kind: Optional[bool]) -> None:
        self.status_var.set(text)

    def _update_status(self) -> None:
        """根据当前 Active Provider 显示 AI 状态（🟢 已配置 / ⚪ 未配置 / 🟡 不完整）。"""
        active = config_store.get_active_provider()
        if active and active.is_complete():
            status = (
                "🟢 已配置\n\n"
                f"Provider：{active.name or '（未填）'}\n"
                f"Model：{active.model or '（未填）'}\n"
                f"API Key：{active.masked_api_key() or '未配置'}"
            )
        elif active and (active.name or active.base_url or active.api_key or active.model):
            status = (
                "🟡 配置不完整\n\n"
                "缺少以下项：\n"
                + "\n".join(
                    f"  - {nm}"
                    for nm, filled in (
                        ("Provider 名称", bool(active.name)),
                        ("API Base URL", bool(active.base_url)),
                        ("API Key", bool(active.api_key)),
                        ("Model", bool(active.model)),
                    )
                    if not filled
                )
                + "\n\n请填写完整后点击「测试连接」验证。"
            )
        else:
            status = "⚪ 未配置\n\n请填写 Provider 名称、API Base URL、API Key、Model，然后点击「测试连接」。"
        self.status_var.set(status)


def show_ai_settings(
    parent: tk.Widget,
    *,
    current: Optional[AiConfig] = None,
    test_fn: Optional[Callable[[AIProviderConfig], object]] = None,
    on_save: Optional[Callable[[AiConfig], None]] = None,
) -> None:
    """便捷入口：弹出 AI 设置对话框（父窗口模态）。"""
    dialog = AiSettingsDialog(
        parent, current=current, test_fn=test_fn, on_save=on_save
    )
    dialog.win.grab_set()
    parent.wait_window(dialog.win)
