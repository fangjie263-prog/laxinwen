"""AI 设置对话框 —— tkinter 独立设置窗口。

对应需求「五、AI 设置窗口」：

- Provider（下拉，可手输任意 OpenAI-compatible Provider 名，不硬编码成只有 OpenAI）
- API Base URL
- API Key（正常 tkinter Entry，默认 ``show="*"``，提供「显示 Key」勾选临时明文查看）
- Model
- 按钮：测试连接 / 保存 / 取消
- 状态区：显示当前配置状态与测试连接结果

安全约束：
- 任何日志 / 对话框文本 / 返回值都**不包含完整 API Key**（只显示掩码）；
- 保存走 ``config_store.save_config`` 写回项目根 .env（保留 CNB_TOKEN 等其它配置）；
- 「测试连接」复用 ``provider.test_connection`` 真正走一次极短模型请求，
  错误被映射成用户可读文案（401 / 404 / 网络错误）。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from .ai.config_store import AiConfig, masked, read_config
from .ai.provider import AIProviderConfig, test_connection

logger = logging.getLogger(__name__)

# 常见 Provider 提示（不硬编码为唯一，允许手输任意值）
_PROVIDER_PRESETS = ("openai-compatible", "openai", "deepseek", "tokenrhythm")


class AiSettingsDialog:
    """AI 设置对话框。构造时立即显示；父窗口自行判断是否需要模态/等待。"""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        current: Optional[AiConfig] = None,
        test_fn: Optional[Callable[[AIProviderConfig], object]] = None,
        on_save: Optional[Callable[[AiConfig], None]] = None,
    ):
        self._parent = parent
        self._test_fn = test_fn or self._default_test
        self._on_save = on_save
        self._test_in_progress = False
        self._last_test_ok = None  # True / False / None(未测)

        # 读取当前配置（保存后即时刷新由外部回调负责）
        cfg = current or read_config()

        self.win = tk.Toplevel(parent)
        self.win.title("AI 设置")
        self.win.geometry("560x520")
        self.win.minsize(520, 480)
        self.win.transient(parent)
        self.win.resizable(True, True)

        self._build_ui(cfg)
        self._update_status()

    # ------------------------------------------------------------------ UI

    def _build_ui(self, cfg: AiConfig) -> None:
        pad = {"padx": 12, "pady": 4}
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill="both", expand=True)

        # ---- Provider ----
        ttk.Label(outer, text="Provider：").pack(anchor="w", **pad)
        self.provider_var = tk.StringVar(value=cfg.provider or "")
        self.provider_combo = ttk.Combobox(
            outer,
            textvariable=self.provider_var,
            values=_PROVIDER_PRESETS,
            width=40,
        )
        self.provider_combo.pack(fill="x", padx=12)
        ttk.Label(
            outer, text="任意 OpenAI-compatible Provider 均可，不限于下拉列表。",
            foreground="#888",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        # ---- Base URL ----
        ttk.Label(outer, text="API Base URL：").pack(anchor="w", **pad)
        self.base_url_var = tk.StringVar(value=cfg.base_url or "")
        self.base_url_entry = ttk.Entry(outer, textvariable=self.base_url_var, width=50)
        self.base_url_entry.pack(fill="x", padx=12)

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
        self.model_var = tk.StringVar(value=cfg.model or "")
        self.model_entry = ttk.Entry(outer, textvariable=self.model_var, width=50)
        self.model_entry.pack(fill="x", padx=12)

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
        ttk.Label(self.status_card, textvariable=self.status_var, justify="left", wraplength=470).pack(
            anchor="w"
        )

    # ------------------------------------------------------------------ 行为

    def _on_toggle_key(self) -> None:
        """临时显示/隐藏 API Key（不改写 Key 内容）。"""
        if self.show_key_var.get():
            self.api_key_entry.configure(show="")
        else:
            self.api_key_entry.configure(show="*")

    def _current_config(self) -> AIProviderConfig:
        """把当前表单组装成 AIProviderConfig（用于测试连接）。"""
        return AIProviderConfig(
            provider=(self.provider_var.get() or "openai-compatible").strip(),
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )

    def _default_test(self, config: AIProviderConfig):
        return test_connection(config)

    def _on_test_connection(self) -> None:
        if self._test_in_progress:
            return
        cfg = self._current_config()
        # 客户端先做最基础校验，避免把空配置交给测试
        if not cfg.base_url or not cfg.api_key or not cfg.model:
            self._set_status(
                "❌ 请先填写 API Base URL、API Key 和 Model 再测试。", kind=False
            )
            return
        self._test_in_progress = True
        self.test_btn.configure(state="disabled")
        self.status_var.set("⏳ 正在测试连接（发送极短请求），请稍候……")

        def worker() -> None:
            try:
                result = self._test_fn(cfg)
            except Exception as exc:  # 测试函数不应抛异常，但防御一下
                self.win.after(0, lambda: self._on_test_done(None, str(exc)))
                return
            self.win.after(0, lambda: self._on_test_done(result, None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, result, err: Optional[str]) -> None:
        self._test_in_progress = False
        self.test_btn.configure(state="normal")
        if err:
            self._last_test_ok = False
            self._set_status(f"❌ 测试失败\n\n{err[:400]}", kind=False)
            return
        ok = bool(getattr(result, "ok", False))
        msg = getattr(result, "message", "") or ("✅ 测试成功" if ok else "❌ 测试失败")
        self._last_test_ok = ok
        self._set_status(msg, kind=ok)

    def _on_save_clicked(self) -> None:
        """保存配置到 .env，并立即回调父窗口刷新（不要求重启）。"""
        from .ai.config_store import save_config

        cfg = AiConfig(
            provider=self.provider_var.get().strip(),
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )
        if not cfg.api_key:
            # 允许 Key 留空保存（保留 .env 中原有 Key），但给出提示
            if not messagebox.askyesno(
                "保存", "API Key 为空。\n\n确定保存吗？（将保留 .env 中已有的 Key）"
            ):
                return
        try:
            path = save_config(cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", f"保存 AI 配置失败：\n{exc}")
            return
        if self._on_save:
            self._on_save(cfg)
        logger.info("AI 配置已保存（API Key 已掩码，不打印）")
        messagebox.showinfo(
            "已保存",
            "AI 配置已保存到：\n"
            f"{path}\n\n"
            "已立即生效，可直接点击“🤖 AI 分析”。",
        )
        self.win.destroy()

    def _set_status(self, text: str, *, kind: Optional[bool]) -> None:
        self.status_var.set(text)

    def _update_status(self) -> None:
        """根据当前 .env 配置显示 AI 状态（🟢 已配置 / ⚪ 未配置 / 🔴 配置存在但测试失败）。"""
        cfg = read_config()
        if cfg.is_complete():
            status = (
                "🟢 已配置\n\n"
                f"Provider：{cfg.provider or '（未填）'}\n"
                f"Model：{cfg.model or '（未填）'}\n"
                f"API Key：{cfg.masked_api_key() or '未配置'}"
            )
            if cfg.uses_cnb:
                status += "\n\n（当前通过 CNB_TOKEN 使用 CNB AI 网关）"
        elif cfg.provider or cfg.base_url or cfg.api_key or cfg.model:
            status = (
                "🟡 配置不完整\n\n"
                "缺少以下项：\n"
                + "\n".join(
                    f"  - {name}"
                    for name, filled in (
                        ("Provider", bool(cfg.provider)),
                        ("API Base URL", bool(cfg.base_url)),
                        ("API Key", bool(cfg.api_key)),
                        ("Model", bool(cfg.model)),
                    )
                    if not filled
                )
                + "\n\n请填写完整后点击「测试连接」验证。"
            )
        else:
            status = "⚪ 未配置\n\n请填写 Provider、API Base URL、API Key、Model，然后点击「测试连接」。"
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
