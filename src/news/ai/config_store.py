"""AI 配置的本地存取层 —— 让普通用户无需打开 PowerShell 也能配置 AI。

设计目标（对应需求「GUI 内置 AI 配置中心」）：

- **最小方案**：只读写项目根 ``.env``，复用现有 ``provider.load_dotenv`` 的读取语义，
  不引入 python-dotenv / 不重新造一套配置机制。
- **不破坏已有 .env**：保存时按 KEY 逐字段更新/追加，**保留其它未知配置**，
  **不删除 CNB_TOKEN**。
- **API Key 安全**：
  - 写入 .env 但不进 Git（仓库 .gitignore 已排除 ``.env`` / ``.env.*``）；
  - 绝不写入 SQLite / HTML 导出 / 日志 / README；
  - 提供 ``masked()`` 只显示掩码（如 ``sk-****abcd``）。
- 支持字段：
    AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL
  同时审计并保留现有其它 AI 配置字段（AI_TIMEOUT / AI_TEMPERATURE / AI_MAX_TOKENS）
  以及 CNB_TOKEN 回退能力。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# GUI 支持的核心 AI 配置字段
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


@dataclass
class AiConfig:
    """一次读取/保存的 AI 配置快照。"""

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


def read_config(env_path: str | Path | None = None) -> AiConfig:
    """从 .env 读取当前 AI 配置（不写入，只读取）。

    只读项目根 .env 中显式配置的值；若 .env 未配置 AI_MODEL/AI_BASE_URL，
    但环境变量存在，也一并纳入（便于在 CLI/CI 场景下展示真实生效配置）。
    """
    import os

    path = Path(env_path) if env_path else default_env_path()
    values: dict[str, str] = {}

    if path.is_file():
        values.update(_parse_env(path.read_text(encoding="utf-8")))

    # 环境变量兜底（GUI 保存前展示的"当前生效配置"）
    for key in AI_KEYS + AI_OPTIONAL_KEYS + (CNB_TOKEN_KEY,):
        env_val = os.environ.get(key, "").strip()
        if env_val:
            values.setdefault(key, env_val)

    cfg = AiConfig(
        provider=values.get("AI_PROVIDER", ""),
        base_url=values.get("AI_BASE_URL", ""),
        api_key=values.get("AI_API_KEY", ""),
        model=values.get("AI_MODEL", ""),
    )
    for key in AI_OPTIONAL_KEYS:
        if key in values:
            cfg.extra[key] = values[key]
    cfg.uses_cnb = bool(values.get(CNB_TOKEN_KEY)) and not values.get("AI_API_KEY")

    return cfg


def save_config(cfg: AiConfig, env_path: str | Path | None = None) -> Path:
    """把 AI 配置安全写回项目根 .env。

    规则（对应需求「配置保存位置」）：
    - 项目根 ``.env`` 不存在则新建；
    - 对 AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL 逐字段更新或追加；
    - 保留其它所有未知配置行（注释 / 其它 KEY= 均原样保留）；
    - **不删除 CNB_TOKEN**；
    - 不把 API Key 写入任何其它位置。
    """
    path = Path(env_path) if env_path else default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    original_lines: list[str] = []
    if path.is_file():
        original_lines = path.read_text(encoding="utf-8").splitlines()

    # 需要写入的四个核心字段
    updates = {
        "AI_PROVIDER": cfg.provider.strip(),
        "AI_BASE_URL": cfg.base_url.strip(),
        "AI_API_KEY": cfg.api_key.strip(),
        "AI_MODEL": cfg.model.strip(),
    }

    result_lines: list[str] = []
    written: set[str] = set()

    def _line_for(key: str, value: str) -> str:
        # 值含空格/特殊字符时用双引号包裹（避免解析歧义）
        if value and any(ch in value for ch in (" ", "#", "'")):
            return f"{key}=\"{value}\""
        return f"{key}={value}"

    # 第一遍：替换已存在的核心字段行，保留其余所有行
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
            # 其它未知配置 / CNB_TOKEN 原样保留
            result_lines.append(line)

    # 第二遍：追加缺失的核心字段
    for key in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
        if key not in written and updates[key]:
            result_lines.append(_line_for(key, updates[key]))
            written.add(key)

    # 防止空配置写空行：仅当至少写入了非空字段时才写文件
    if not written:
        logger.info("没有可保存的 AI 配置，跳过写入 %s", path)
        return path

    body = "\n".join(result_lines).strip() + "\n"
    path.write_text(body, encoding="utf-8")
    logger.info("AI 配置已保存到 %s（API Key 不写入日志）", path)
    return path


def masked(value: str) -> str:
    """工具函数：对任意字符串做掩码（默认只留首尾 4 位）。"""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]
