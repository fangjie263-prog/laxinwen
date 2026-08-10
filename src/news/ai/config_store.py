"""AI 配置的本地存取层 —— 支持**多个 AI Provider** 的托管与切换。

本模块升级为「AI Provider 配置管理器」：

- 可保存 / 读取 / 删除 / 切换**多个 Provider**；
- 每个 Provider 独立存储 Base URL / API Key / Model；
- 通过 ``AI_ACTIVE_PROVIDER`` 标记当前生效的 Provider；
- 通过 ``AI_PROVIDER_<NAME>_BASE_URL / _API_KEY / _MODEL`` 存储每个 Provider 的配置；
- 兼容旧版单 Provider 配置（``AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL``），
  首次读取时自动迁移为 Active Provider，不丢失旧配置、无需重新输入 Key。

安全约束（保持不变）：
- API Key 只写入项目根 ``.env``（仓库 ``.gitignore`` 已排除 ``.env`` / ``.env.*``）；
- API Key 绝不写入 SQLite / HTML 导出 / 日志 / README / Git；
- 提供 ``masked()`` 只显示掩码（如 ``sk-****abcd``）；
- 保存时按 KEY 逐字段更新/追加，**保留其它未知配置**，**不删除 CNB_TOKEN**。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# GUI 支持的核心 AI 配置字段（旧版单 Provider 配置，用于向后兼容/迁移）
AI_KEYS = (
    "AI_PROVIDER",
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
)

# 审计到的其它 AI 相关可选字段（保留，不删除）
AI_OPTIONAL_KEYS = (
    "AI_TIMEOUT",
    "AI_TEMPERATURE",
    "AI_MAX_TOKENS",
)

# 特殊字段：CNB_TOKEN 用于在 CNB 流水线内调用 CNB AI 网关，必须保留
CNB_TOKEN_KEY = "CNB_TOKEN"

# Active Provider 标记
ACTIVE_PROVIDER_KEY = "AI_ACTIVE_PROVIDER"

# 多 Provider 存储前缀 / 段
_PROVIDER_PREFIX = "AI_PROVIDER_"
_PROVIDER_SUFFIXES = ("_BASE_URL", "_API_KEY", "_MODEL")

# 预设 Provider（内置，自动填充 Base URL / 模型候选；不写死模型）
PRESET_PROVIDERS = ("openai", "gemini", "tokenrhythm")

# 预设 Provider 的默认 Base URL
_PRESET_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "tokenrhythm": "https://tokenrhythm.studio/v1",
}

# 预设 Provider 的常用模型候选（仅作下拉建议，不写死；以 Provider /models 返回为准）
_PRESET_MODEL_CANDIDATES = {
    "openai": ["gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4o", "gpt-4o-mini"],
    "gemini": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
    "tokenrhythm": ["deepseek-v4-flash", "deepseek-v3.2", "deepseek-chat"],
}

# 合法 Provider key 段（用于构建 env 变量名）：只允许字母数字下划线
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_]")


@dataclass
class AiConfig:
    """一次读取/保存的 AI 配置快照（Active Provider 的当前配置）。"""

    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 其它 .env 里审计到的 AI 相关字段（原样保留）
    extra: dict[str, str] = field(default_factory=dict)
    # 当前是否已通过 CNB_TOKEN 走 CNB AI 网关（用于状态提示）
    uses_cnb: bool = False

    def is_complete(self) -> bool:
        """核心四项是否都已填写（可用于判定"已配置"）。"""
        return bool(self.provider and self.base_url and self.api_key and self.model)

    def masked_api_key(self) -> str:
        """安全掩码：sk-****abcd；为空返回空字符串。"""
        key = (self.api_key or "").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "…" + key[-4:]


@dataclass
class ProviderConfig:
    """单个 AI Provider 的完整配置。"""

    name: str = ""           # 显示名（如 tokenrhythm / My Company API）
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    def is_complete(self) -> bool:
        return bool(self.name and self.base_url and self.api_key and self.model)

    def masked_api_key(self) -> str:
        key = (self.api_key or "").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "…" + key[-4:]

    def to_ai_config(self) -> AiConfig:
        return AiConfig(
            provider=self.name,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
        )


def default_env_path() -> Path:
    """默认 .env 路径：项目根。"""
    # 项目根：src/news/ai/config_store.py 向上三级
    return Path(__file__).resolve().parents[3] / ".env"


def _parse_env(text: str) -> dict[str, str]:
    """把 .env 文本解析为 dict（保留所有键，忽略注释/空行/无= 行）。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip("'\"")
    return out


def _read_values(env_path: str | Path | None = None) -> dict[str, str]:
    """读取 .env 全部键值，并用 os.environ 兜底（不覆盖 .env 已有值）。"""
    import os

    path = Path(env_path) if env_path else default_env_path()
    values: dict[str, str] = {}
    if path.is_file():
        values.update(_parse_env(path.read_text(encoding="utf-8")))
    for key in os.environ:
        if key.startswith("AI_") or key == CNB_TOKEN_KEY:
            values.setdefault(key, os.environ[key].strip())
    return values


def _safe_key(name: str) -> str:
    """把 Provider 显示名规范化为 env key 使用的安全段（大写字母数字下划线）。"""
    s = name.strip()
    if not s:
        return ""
    s = _SAFE_KEY_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.upper()


def _provider_env_keys(name: str) -> tuple[str, str, str]:
    """返回某 Provider 的三个 env key：BASE_URL / API_KEY / MODEL。"""
    key = _safe_key(name)
    return (
        f"{_PROVIDER_PREFIX}{key}_BASE_URL",
        f"{_PROVIDER_PREFIX}{key}_API_KEY",
        f"{_PROVIDER_PREFIX}{key}_MODEL",
    )


# ---------------------------------------------------------------------------
# 多 Provider 存储层
# ---------------------------------------------------------------------------

def _list_provider_names_from(values: dict[str, str]) -> list[str]:
    """从 .env 键值里解析出所有 Provider 名（含旧版单 Provider 配置迁移名）。"""
    names: list[str] = []

    # 1) 多 Provider 段：AI_PROVIDER_<NAME>_BASE_URL / _API_KEY / _MODEL
    for key in values:
        if key.startswith(_PROVIDER_PREFIX) and key.endswith(_PROVIDER_SUFFIXES):
            # 去掉前缀和已知后缀，剩下来的是 Provider key 段
            mid = key[len(_PROVIDER_PREFIX):]
            for suffix in _PROVIDER_SUFFIXES:
                if mid.endswith(suffix):
                    mid = mid[: -len(suffix)]
                    break
            if mid and mid not in names:
                names.append(mid)

    # 2) 旧版单 Provider：AI_PROVIDER 里记录的名字
    legacy = (values.get("AI_PROVIDER", "") or "").strip()
    if legacy and _safe_key(legacy) not in names:
        names.append(_safe_key(legacy))

    return names


def list_providers(env_path: str | Path | None = None) -> list[ProviderConfig]:
    """列出当前保存的所有 Provider 配置（含自动迁移的旧版配置）。

    返回按名称排序；不含空的 Provider 条目。
    """
    values = _read_values(env_path)
    names = _list_provider_names_from(values)
    providers: list[ProviderConfig] = []
    seen: set[str] = set()

    for name in names:
        # 优先尝试多 Provider 段，否则尝试旧版单 Provider
        cfg = _provider_from_env_key(values, name) or _legacy_provider(values)
        if cfg and cfg.name and cfg.name not in seen:
            seen.add(cfg.name)
            providers.append(cfg)

    providers.sort(key=lambda p: (p.name.lower(), p.name))
    return providers


def _legacy_provider(values: dict[str, str]) -> Optional[ProviderConfig]:
    """读取旧版单 Provider 配置（AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL）。"""
    provider = (values.get("AI_PROVIDER", "") or "").strip()
    base_url = (values.get("AI_BASE_URL", "") or "").strip()
    api_key = (values.get("AI_API_KEY", "") or "").strip()
    model = (values.get("AI_MODEL", "") or "").strip()
    if not (provider or base_url or api_key or model):
        return None
    return ProviderConfig(
        name=provider or "openai-compatible",
        base_url=base_url,
        api_key=api_key,
        model=model,
    )


def _provider_from_env_key(values: dict[str, str], name: str) -> Optional[ProviderConfig]:
    """从多 Provider 段读取单个 Provider 配置。

    ``name`` 为规范化 key 段（如 OPENAI）；显示名优先用旧版 ``AI_PROVIDER``
    记录的原始值（保证向后兼容与大小写保真）。
    """
    key = _safe_key(name)
    if not key:
        return None
    base_url, api_key, model = _provider_env_keys(name)
    cfg = ProviderConfig(
        name=key,  # 默认显示名 = 规范化 key 段
        base_url=(values.get(base_url, "") or "").strip(),
        api_key=(values.get(api_key, "") or "").strip(),
        model=(values.get(model, "") or "").strip(),
    )
    # 显示名还原：优先用旧版 AI_PROVIDER 的原始值；其次预设名（小写）
    legacy_name = (values.get("AI_PROVIDER", "") or "").strip()
    if legacy_name and _safe_key(legacy_name) == key:
        cfg.name = legacy_name
    else:
        preset = next(
            (p for p in PRESET_PROVIDERS if _safe_key(p) == key), None
        )
        if preset:
            cfg.name = preset
    if not (cfg.base_url or cfg.api_key or cfg.model):
        return None
    return cfg


def get_provider(name: str, env_path: str | Path | None = None) -> Optional[ProviderConfig]:
    """按名称读取单个 Provider 配置；不存在返回 None。"""
    if not name.strip():
        return None
    values = _read_values(env_path)
    wanted = _safe_key(name)
    for p in list_providers_from_values(values):
        if _safe_key(p.name) == wanted:
            return p
    return None


def list_providers_from_values(values: dict[str, str]) -> list[ProviderConfig]:
    """从已解析的键值里列出 Provider（供内部复用）。"""
    names = _list_provider_names_from(values)
    providers: list[ProviderConfig] = []
    seen: set[str] = set()
    for name in names:
        cfg = _provider_from_env_key(values, name) or _legacy_provider(values)
        if cfg and _safe_key(cfg.name) not in seen:
            seen.add(_safe_key(cfg.name))
            providers.append(cfg)
    providers.sort(key=lambda p: (p.name.lower(), p.name))
    return providers


def get_active_provider(env_path: str | Path | None = None) -> ProviderConfig:
    """返回当前 Active Provider（有则返回；没有则自动迁移旧配置或返回第一个）。"""
    values = _read_values(env_path)
    active_name = (values.get(ACTIVE_PROVIDER_KEY, "") or "").strip()
    providers = list_providers_from_values(values)

    if active_name:
        for p in providers:
            if _safe_key(p.name) == _safe_key(active_name):
                return p
    if providers:
        return providers[0]
    return ProviderConfig()


def get_active_provider_name(env_path: str | Path | None = None) -> str:
    """返回当前 Active Provider 的显示名（未设置时返回第一个 Provider 名）。"""
    values = _read_values(env_path)
    active_name = (values.get(ACTIVE_PROVIDER_KEY, "") or "").strip()
    providers = list_providers_from_values(values)
    if active_name and any(_safe_key(p.name) == _safe_key(active_name) for p in providers):
        return next(p.name for p in providers if _safe_key(p.name) == _safe_key(active_name))
    if providers:
        return providers[0].name
    return ""


# ---------------------------------------------------------------------------
# 旧版兼容：read_config / save_config 仍返回/写入 Active Provider
# ---------------------------------------------------------------------------

def read_config(env_path: str | Path | None = None) -> AiConfig:
    """从 .env 读取当前 Active Provider 的配置（不写入，只读取）。

    向后兼容：
    - 若只有旧版单 Provider 配置，自动视其为 Active Provider 返回；
    - 若存在 AI_ACTIVE_PROVIDER，返回对应 Provider。
    """
    active = get_active_provider(env_path)
    if active and (active.base_url or active.api_key or active.model or active.name):
        cfg = active.to_ai_config()
        values = _read_values(env_path)
        for key in AI_OPTIONAL_KEYS:
            if key in values:
                cfg.extra[key] = values[key]
        # CNB 网关回退提示
        cfg.uses_cnb = bool(values.get(CNB_TOKEN_KEY)) and not cfg.api_key
        return cfg
    return AiConfig()


def save_config(cfg: AiConfig, env_path: str | Path | None = None) -> Path:
    """把 Active Provider 配置安全写回项目根 .env（保留旧单 Provider 兼容字段）。

    规则：
    - 把 ``cfg.provider`` 作为 Active Provider，写入：
        AI_ACTIVE_PROVIDER=<name>
        AI_PROVIDER_<NAME>_BASE_URL / _API_KEY / _MODEL
    - 同时回写旧版字段 AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL，
      保证只读取旧版字段的旧代码也能继续工作；
    - 保留其它所有未知配置行（注释 / 其它 KEY= 均原样保留）；
    - **不删除 CNB_TOKEN**。
    """
    path = Path(env_path) if env_path else default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    original_lines: list[str] = []
    if path.is_file():
        original_lines = path.read_text(encoding="utf-8").splitlines()

    provider = cfg.provider.strip() or "openai-compatible"

    # 需要写入的字段（旧版字段 + 多 Provider 段 + active 标记）
    b_url, b_key, b_model = _provider_env_keys(provider)
    updates = {
        "AI_PROVIDER": provider,
        "AI_BASE_URL": cfg.base_url.strip(),
        "AI_API_KEY": cfg.api_key.strip(),
        "AI_MODEL": cfg.model.strip(),
        ACTIVE_PROVIDER_KEY: provider,
        b_url: cfg.base_url.strip(),
        b_key: cfg.api_key.strip(),
        b_model: cfg.model.strip(),
    }

    result_lines: list[str] = []
    written: set[str] = set()

    def _line_for(key: str, value: str) -> str:
        if value and any(ch in value for ch in (" ", "#", "'")):
            return f'{key}="{value}"'
        return f"{key}={value}"

    for line in original_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            result_lines.append(line)
            continue
        key, _, _value = stripped.partition("=")
        key = key.strip()
        if key in updates:
            result_lines.append(_line_for(key, updates[key]))
            written.add(key)
        else:
            result_lines.append(line)

    for key, value in updates.items():
        if key not in written and value:
            result_lines.append(_line_for(key, value))
            written.add(key)

    if not written:
        logger.info("没有可保存的 AI 配置，跳过写入 %s", path)
        return path

    body = "\n".join(result_lines).strip() + "\n"
    path.write_text(body, encoding="utf-8")
    logger.info("AI 配置已保存到 %s（API Key 不写入日志）", path)
    return path


def save_provider(cfg: ProviderConfig, env_path: str | Path | None = None) -> Path:
    """保存单个 Provider（不改变 Active Provider）。"""
    return save_config(cfg.to_ai_config(), env_path)


def delete_provider(name: str, env_path: str | Path | None = None) -> bool:
    """删除单个 Provider 配置；只删除该 Provider，不影响其它 Provider / 旧字段。

    - 若删除的是 Active Provider，自动把 Active 切换到剩余的第一个 Provider；
    - 若没有剩余 Provider，则清空 Active 标记；
    - 返回是否删除了某个 Provider。
    """
    if not name.strip():
        return False
    path = Path(env_path) if env_path else default_env_path()
    wanted = _safe_key(name)
    values = _read_values(path)
    providers = list_providers_from_values(values)
    target = next((p for p in providers if _safe_key(p.name) == wanted), None)
    if target is None:
        return False

    original_lines: list[str] = []
    if path.is_file():
        original_lines = path.read_text(encoding="utf-8").splitlines()

    # 需要删除的 env key（多 Provider 段）
    del_keys = set(_provider_env_keys(target.name))

    remaining = [p for p in providers if _safe_key(p.name) != wanted]

    result_lines: list[str] = []
    for line in original_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            result_lines.append(line)
            continue
        key, _, _ = stripped.partition("=")
        key = key.strip()
        if key in del_keys:
            continue  # 删除该 Provider 的段
        result_lines.append(line)

    # 处理 Active 标记：若删除的是 Active Provider，切换到剩余的第一个
    active_name = (values.get(ACTIVE_PROVIDER_KEY, "") or "").strip()
    if active_name and _safe_key(active_name) == wanted:
        new_active = remaining[0].name if remaining else ""
        # 移除旧的 active 行，写回新 active（或无）
        result_lines = [
            line for line in result_lines
            if not line.lstrip().startswith(ACTIVE_PROVIDER_KEY + "=")
        ]
        if new_active:
            result_lines.append(f"{ACTIVE_PROVIDER_KEY}={new_active}")
            # 同步旧字段到新 active
            _sync_legacy_from(result_lines, new_active)
        else:
            # 无剩余 Provider：清空旧版单 Provider 字段，避免旧代码读到残留配置
            result_lines = [
                line for line in result_lines
                if not line.lstrip().startswith(("AI_PROVIDER=", "AI_BASE_URL=",
                                                  "AI_API_KEY=", "AI_MODEL="))
            ]

    body = "\n".join(result_lines).strip() + "\n"
    if body.strip():
        path.write_text(body, encoding="utf-8")
    else:
        # 空文件则删除
        try:
            path.unlink()
        except OSError:
            pass
    logger.info("已删除 Provider: %s（API Key 不写入日志）", target.name)
    return True


def _sync_legacy_from(result_lines: list[str], provider_name: str) -> None:
    """在删除 Active Provider 切换后，把旧版单 Provider 字段同步为新的 active。"""
    values = {}
    for line in result_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        values[k.strip()] = v.strip().strip("'\"")
    b_url, b_key, b_model = _provider_env_keys(provider_name)
    legacy = {
        "AI_PROVIDER": provider_name,
        "AI_BASE_URL": values.get(b_url, ""),
        "AI_API_KEY": values.get(b_key, ""),
        "AI_MODEL": values.get(b_model, ""),
    }
    out: list[str] = []
    for line in result_lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        k, _, _ = stripped.partition("=")
        k = k.strip()
        if k in legacy:
            v = legacy[k]
            out.append(f'{k}="{v}"' if v and any(c in v for c in (" ", "#", "'")) else f"{k}={v}")
        else:
            out.append(line)
    result_lines[:] = out


def apply_to_env(cfg: AiConfig) -> None:
    """把 Active Provider 配置同步到当前进程的 ``os.environ``（**覆盖**已有值）。

    用途：让 ``save_config`` / 测试成功自动保存后立即在当前进程生效，无需重启 GUI。
    只改内存中的 os.environ，不写回 .env；API Key 只在内存中写入，不打印。
    """
    import os

    mapping = {
        "AI_PROVIDER": cfg.provider,
        "AI_BASE_URL": cfg.base_url,
        "AI_API_KEY": cfg.api_key,
        "AI_MODEL": cfg.model,
    }
    for key, value in mapping.items():
        value = (value or "").strip()
        if value:
            os.environ[key] = value


def masked(value: str) -> str:
    """工具函数：对任意字符串做掩码（默认只留首尾 4 位）。"""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]


# ---------------------------------------------------------------------------
# 预设 Provider 辅助
# ---------------------------------------------------------------------------

def preset_base_url(name: str) -> str:
    """返回预设 Provider 的默认 Base URL；非预设返回空。"""
    return _PRESET_BASE_URL.get(name.strip().lower(), "")


def preset_model_candidates(name: str) -> list[str]:
    """返回预设 Provider 的常用模型候选（仅作下拉建议，不写死）。"""
    return list(_PRESET_MODEL_CANDIDATES.get(name.strip().lower(), []))


def is_preset(name: str) -> bool:
    """判断某 Provider 名是否为预设 Provider。"""
    return name.strip().lower() in PRESET_PROVIDERS


def preset_provider_names() -> list[str]:
    """返回预设 Provider 名列表（供下拉展示）。"""
    return list(PRESET_PROVIDERS)
