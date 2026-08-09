"""AI Provider 抽象层 —— 与任何具体厂商解耦。

设计目标：
- 业务逻辑（processor / CLI / storage）只依赖 BaseProvider 与 AIProviderConfig，
  不感知任何具体厂商。
- 通过环境变量配置，允许在 TokenRhythm、DeepSeek、CNB AI 网关或任何
  OpenAI-compatible endpoint 之间无缝切换。
- API Key 只从环境变量 / .env 读取，绝不进入代码、YAML、Git。

环境变量：
    AI_PROVIDER    provider 标识（默认 openai-compatible）
    AI_BASE_URL    OpenAI-compatible base URL（如 https://tokenrhythm.studio/v1）
    AI_API_KEY     API Key（默认回退到 CNB_TOKEN，用于 CNB AI 网关）
    AI_MODEL       模型名（默认 deepseek-v4-flash，但不写死在业务逻辑）
    AI_TIMEOUT     请求超时秒数（默认 60）
    AI_TEMPERATURE 采样温度（默认 0.2，可空）
    AI_MAX_TOKENS  输出最大 token（默认 4000，可空）
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Provider 名。未配置 AI_PROVIDER 时默认 OpenA-compatible。
DEFAULT_PROVIDER = "openai-compatible"


class AIProviderError(Exception):
    """AI Provider 层错误（网络 / HTTP / 认证 / 空响应等）。"""


@dataclass
class AIProviderConfig:
    """AI Provider 配置（全部来自环境变量 / .env）。"""

    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 4000

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "AIProviderConfig":
        """从环境变量构建配置。未设置时给出明确错误信息。"""
        env = env if env is not None else os.environ

        provider = env.get("AI_PROVIDER", "").strip() or DEFAULT_PROVIDER
        base_url = env.get("AI_BASE_URL", "").strip()
        api_key = env.get("AI_API_KEY", "").strip()
        model = env.get("AI_MODEL", "").strip()

        if not api_key and not base_url:
            # CNB 流水线内可直接用 CNB_TOKEN 访问 CNB AI 网关
            cnb_token = env.get("CNB_TOKEN", "").strip()
            if cnb_token:
                api_key = cnb_token
                if not base_url:
                    base_url = _default_cnb_base_url(env)
                if not model:
                    model = "deepseek-v4-flash"

        def _float(name: str, default: float) -> float:
            try:
                return float(env.get(name, "").strip() or default)
            except ValueError:
                logger.warning("%s 不是合法数字，使用默认 %.1f", name, default)
                return default

        def _int(name: str, default: Optional[int]) -> Optional[int]:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                logger.warning("%s 不是合法整数，忽略", name)
                return default

        timeout = _float("AI_TIMEOUT", 60.0)
        temp_raw = env.get("AI_TEMPERATURE", "").strip()
        temperature: Optional[float] = (
            _float("AI_TEMPERATURE", 0.2) if temp_raw else 0.2
        )
        max_tokens = _int("AI_MAX_TOKENS", 4000)

        return cls(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def validate(self) -> None:
        """校验配置完整性，缺失时抛 AIProviderError。"""
        if not self.model:
            raise AIProviderError(
                "缺少 AI_MODEL 环境变量。请配置 AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL，"
                "或用 CNB_TOKEN 在 CNB 流水线内调用 CNB AI 网关。"
            )
        if not self.base_url:
            raise AIProviderError(
                "缺少 AI_BASE_URL 环境变量（OpenAI-compatible endpoint）。"
            )
        if not self.api_key:
            raise AIProviderError(
                "缺少 AI_API_KEY（或 CNB_TOKEN）。API Key 只能通过环境变量 / .env 提供，"
                "绝不能写入代码或 Git。"
            )

    def redacted(self) -> dict:
        """用于日志 / 报告的安全表示（隐藏 Key）。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "api_key": (self.api_key[:4] + "…" + self.api_key[-4:]) if self.api_key else "",
        }


def _default_cnb_base_url(env: dict[str, str]) -> str:
    """CNB AI 网关默认 base_url（形如 https://api.cnb.cool/<repo>/-/ai）。

    优先使用 CNB 流水线环境变量推导，否则给出占位说明。
    """
    root = env.get("CNB_ROOT_SLUG", "").strip() or env.get("CNB_GROUP_SLUG", "").strip()
    repo = env.get("CNB_REPO_NAME", "").strip()
    if root and repo:
        return f"https://api.cnb.cool/{root}/{repo}/-/ai"
    return "https://api.cnb.cool"


@dataclass
class ProviderResult:
    """一次 AI 调用的规范化结果（与厂商无关）。"""

    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)   # 原始 usage，含 token / cost（如有）
    raw: str = ""                               # 原始响应体（便于调试）


class BaseProvider(ABC):
    """AI Provider 接口。所有具体 Provider 都实现该接口。"""

    @abstractmethod
    def chat(self, *, system: str, user: str) -> ProviderResult:
        """调用模型，返回纯文本内容。业务层负责 JSON 解析与 schema 校验。"""


def build_provider(config: Optional[AIProviderConfig] = None) -> BaseProvider:
    """根据配置构建 Provider。当前实现 OpenAI-compatible Provider。"""
    from .openai_compatible import OpenAICompatibleProvider

    cfg = config or AIProviderConfig.from_env()
    cfg.validate()
    return OpenAICompatibleProvider(cfg)


def load_dotenv(path: str | Path | None = None) -> None:
    """轻量 .env 加载器（不引入 python-dotenv 依赖）。

    规则：
    - 默认读取项目根 .env（若存在）；
    - 只设置当前环境中不存在的变量（不覆盖已有值）；
    - 支持 KEY=VALUE、# 注释、引号剥离。
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        # 项目根（src/news/ai 向上三级）
        candidates.append(Path(__file__).resolve().parents[3] / ".env")
        candidates.append(Path.cwd() / ".env")

    for env_path in candidates:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        logger.info("已加载 .env: %s", env_path)
        return
