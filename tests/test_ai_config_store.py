"""AI 配置存取 + 「测试连接」的离线测试。

覆盖验收清单：
- AI 配置读写 .env（更新字段 / 追加缺失 / 保留其它配置 / 不删 CNB_TOKEN）
- API Key masked
- 日志不出现 API Key
- test_connection：401 → 显示 API Key 无效；404/model_not_found → Model 错误；
  网络错误 → 连接错误；成功 → 成功
- 不把 API Key 写入 SQLite / HTML / 日志

所有测试不访问真实网络（mock Provider）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.ai import config_store  # noqa: E402
from news.ai.config_store import (  # noqa: E402
    AI_KEYS,
    AiConfig,
    apply_to_env,
    masked,
    read_config,
    save_config,
)
from news.ai.provider import (  # noqa: E402
    AIProviderConfig,
    AIProviderError,
    test_connection as _test_connection,
)


class TestConfigStore:
    def test_read_empty_env_returns_empty(self, tmp_path):
        cfg = read_config(tmp_path / "no.env")
        assert cfg.provider == ""
        assert cfg.base_url == ""
        assert cfg.api_key == ""
        assert cfg.model == ""
        assert not cfg.is_complete()

    def test_save_creates_env_and_reads_back(self, tmp_path):
        env = tmp_path / ".env"
        cfg = AiConfig(
            provider="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="sk-test123456",
            model="test-model",
        )
        save_config(cfg, env)
        assert env.exists()
        body = env.read_text(encoding="utf-8")
        assert "AI_PROVIDER=openai-compatible" in body
        assert "AI_BASE_URL=https://api.example.com/v1" in body
        assert "AI_API_KEY=sk-test123456" in body
        assert "AI_MODEL=test-model" in body

        loaded = read_config(env)
        assert loaded.provider == "openai-compatible"
        assert loaded.base_url == "https://api.example.com/v1"
        assert loaded.api_key == "sk-test123456"
        assert loaded.model == "test-model"
        assert loaded.is_complete()

    def test_save_updates_existing_field_not_duplicate(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "AI_PROVIDER=old\nAI_BASE_URL=https://old.example/v1\nAI_API_KEY=oldkey\n"
            "AI_MODEL=oldmodel\n# 保留注释\nSOME_OTHER=keep\n",
            encoding="utf-8",
        )
        cfg = AiConfig(
            provider="openai-compatible",
            base_url="https://new.example/v1",
            api_key="newkey",
            model="newmodel",
        )
        save_config(cfg, env)
        body = env.read_text(encoding="utf-8")
        # 保留其它配置与注释
        assert "# 保留注释" in body
        assert "SOME_OTHER=keep" in body
        # 字段已更新，不重复
        assert body.count("AI_PROVIDER=") == 1
        assert body.count("AI_API_KEY=") == 1
        assert "AI_PROVIDER=openai-compatible" in body
        assert "AI_BASE_URL=https://new.example/v1" in body

    def test_save_preserves_cnb_token(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "AI_API_KEY=oldkey\nCNB_TOKEN=cnb-secret-token\n", encoding="utf-8"
        )
        cfg = AiConfig(
            provider="openai-compatible",
            base_url="https://new.example/v1",
            api_key="newkey",
            model="m",
        )
        save_config(cfg, env)
        body = env.read_text(encoding="utf-8")
        assert "CNB_TOKEN=cnb-secret-token" in body  # 不删 CNB_TOKEN
        assert "AI_API_KEY=newkey" in body

    def test_save_appends_missing_fields(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("AI_PROVIDER=p\nAI_MODEL=m\n", encoding="utf-8")
        cfg = AiConfig(provider="p", base_url="", api_key="", model="m")
        save_config(cfg, env)
        body = env.read_text(encoding="utf-8")
        # 空字段不追加；已有字段保留
        assert "AI_PROVIDER=p" in body
        assert "AI_MODEL=m" in body
        assert "AI_API_KEY=" not in body

    def test_masked_api_key(self):
        assert masked("") == ""
        assert masked("sk-test123456") == "sk-t…3456"
        assert masked("short") == "*****"

    def test_key_never_in_logs(self, tmp_path, caplog):
        env = tmp_path / ".env"
        cfg = AiConfig(
            provider="p", base_url="https://x/v1", api_key="SUPERSECRETKEY", model="m"
        )
        with caplog.at_level(logging.INFO):
            save_config(cfg, env)
            read_config(env)
        log_text = caplog.text
        assert "SUPERSECRETKEY" not in log_text


class TestTestConnection:
    """「测试连接」错误映射（mock Provider，不访问真实网络）。"""

    def _cfg(self, **kw):
        defaults = dict(
            provider="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="test-model",
        )
        defaults.update(kw)
        return AIProviderConfig(**defaults)

    def _patch_chat(self, monkeypatch, exc=None, content="OK"):
        from news.ai import openai_compatible as oc
        from news.ai.provider import BaseProvider, ProviderResult

        class FakeProvider(BaseProvider):
            def chat(self, *, system, user):
                if exc:
                    raise exc
                return ProviderResult(content=content, model="test-model")

            def close(self):
                pass

        monkeypatch.setattr(
            "news.ai.provider.build_provider", lambda cfg: FakeProvider()
        )

    def test_success(self, monkeypatch):
        self._patch_chat(monkeypatch)
        res = _test_connection(self._cfg())
        assert res.ok is True
        assert "✅ 测试成功" in res.message
        assert "Provider" in res.message
        assert "API Key：有效" in res.message

    def test_401_maps_to_invalid_key(self, monkeypatch):
        self._patch_chat(monkeypatch, exc=AIProviderError("AI HTTP 401: unauthorized"))
        res = _test_connection(self._cfg())
        assert res.ok is False
        assert res.kind == "auth"
        assert "HTTP 401" in res.message
        assert "API Key 无效或未授权" in res.message

    def test_404_maps_to_model_error(self, monkeypatch):
        self._patch_chat(
            monkeypatch, exc=AIProviderError("AI HTTP 404: model_not_found")
        )
        res = _test_connection(self._cfg())
        assert res.ok is False
        assert res.kind == "model"
        assert "HTTP 404" in res.message
        assert "Model 不存在" in res.message

    def test_network_error_maps_to_connection(self, monkeypatch):
        self._patch_chat(
            monkeypatch, exc=AIProviderError("AI 网络错误: connect timed out")
        )
        res = _test_connection(self._cfg())
        assert res.ok is False
        assert res.kind == "connection"
        assert "无法连接 API Base URL" in res.message

    def test_empty_content_is_model_error(self, monkeypatch):
        self._patch_chat(monkeypatch, content="")
        res = _test_connection(self._cfg())
        assert res.ok is False
        assert res.kind == "model"
        assert "空内容" in res.message

    def test_missing_config_returns_config_error(self, monkeypatch):
        self._patch_chat(monkeypatch)
        res = _test_connection(self._cfg(api_key="", base_url=""))
        assert res.ok is False
        assert res.kind == "config"

    def test_api_key_never_in_result_message(self, monkeypatch):
        secret = "sk-TOP-SECRET-12345"
        self._patch_chat(monkeypatch, exc=AIProviderError("AI HTTP 500: boom"))
        res = _test_connection(self._cfg(api_key=secret))
        # 即使未知错误回显 err，也绝不含完整 Key
        assert secret not in res.message


class TestApplyToEnv:
    """apply_to_env：保存后把配置同步到当前进程 os.environ，立即生效（无需重启）。"""

    def _clear_ai_env(self, monkeypatch):
        for k in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
            monkeypatch.delenv(k, raising=False)

    def test_overwrites_existing_stale_env(self, monkeypatch):
        """关键：覆盖已存在的旧环境变量，让 AI 分析读到新配置。"""
        self._clear_ai_env(monkeypatch)
        monkeypatch.setenv("AI_PROVIDER", "openai")
        monkeypatch.setenv("AI_BASE_URL", "https://old.example/v1")
        monkeypatch.setenv("AI_API_KEY", "old-stale-key")
        monkeypatch.setenv("AI_MODEL", "old-model")

        cfg = AiConfig(
            provider="tokenrhythm",
            base_url="https://tokenrhythm.studio/v1",
            api_key="new-secret-key",
            model="deepseek-v4-flash",
        )
        apply_to_env(cfg)

        # AI 分析（from_env）应读到新配置
        from news.ai.provider import AIProviderConfig

        fe = AIProviderConfig.from_env()
        assert fe.provider == "tokenrhythm"
        assert fe.base_url == "https://tokenrhythm.studio/v1"
        assert fe.api_key == "new-secret-key"
        assert fe.model == "deepseek-v4-flash"

    def test_sets_env_when_absent(self, monkeypatch):
        """进程启动时 os.environ 没有 AI 配置：apply 后也能立即读到。"""
        self._clear_ai_env(monkeypatch)
        cfg = AiConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key="sk-new",
            model="deepseek-chat",
        )
        apply_to_env(cfg)
        from news.ai.provider import AIProviderConfig

        fe = AIProviderConfig.from_env()
        assert fe.provider == "deepseek"
        assert fe.model == "deepseek-chat"

    def test_empty_field_does_not_clear_existing(self, monkeypatch):
        """字段留空时不覆盖 os.environ 旧值（如保存时 Key 留空保留已有 Key）。"""
        self._clear_ai_env(monkeypatch)
        monkeypatch.setenv("AI_API_KEY", "existing-key")
        cfg = AiConfig(
            provider="tokenrhythm",
            base_url="https://tokenrhythm.studio/v1",
            api_key="",  # 留空
            model="deepseek-v4-flash",
        )
        apply_to_env(cfg)
        from news.ai.provider import AIProviderConfig

        fe = AIProviderConfig.from_env()
        assert fe.api_key == "existing-key"  # 保留
        assert fe.provider == "tokenrhythm"

    def test_key_never_logged(self, monkeypatch, caplog):
        self._clear_ai_env(monkeypatch)
        with caplog.at_level(logging.INFO):
            apply_to_env(
                AiConfig(
                    provider="p",
                    base_url="https://x/v1",
                    api_key="SK-TOP-SECRET-ABC",
                    model="m",
                )
            )
        assert "SK-TOP-SECRET-ABC" not in caplog.text


class TestKeyNotLeaked:
    """API Key 不写入 SQLite / HTML / 日志 / README。"""

    def test_key_not_in_exported_html(self, tmp_path):
        # 直接验证 config_store 不写 HTML / SQLite（本模块只写 .env）
        env = tmp_path / ".env"
        secret = "sk-super-secret"
        save_config(AiConfig(provider="p", base_url="https://x/v1", api_key=secret, model="m"), env)
        # 只有 .env 一个文件被写入，且其中含有 Key（这是唯一合法存放点）
        files = list(tmp_path.iterdir())
        assert [f.name for f in files] == [".env"]
        # 不产生任何 .html / .db 文件
        assert not any(f.suffix in (".html", ".db") for f in files)
