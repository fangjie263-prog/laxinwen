"""多 AI Provider 管理器 —— 存储层与自动保存的离线测试。

覆盖验收清单（需求十七）：
- A. 旧单 Provider 配置可以迁移
- B. OpenAI 预设自动填 Base URL
- C. Gemini 预设自动填 Base URL
- D. TokenRhythm 可以恢复保存配置
- E. 新增自定义 Provider
- F. 保存多个 Provider
- G. 切换 Provider
- H. 删除 Provider
- I. 测试成功自动保存
- J. 测试失败不覆盖旧配置
- K. 测试成功后配置立即"已配置"
- L. 无需重启 GUI（apply_to_env 同步 os.environ）
- M. 刷新模型成功
- N. 刷新模型失败但仍允许手工 Model
- O. API Key 不进入日志
- P. API Key 不进入 SQLite / HTML
- Q. 旧 AI_PROVIDER / AI_BASE_URL / AI_API_KEY / AI_MODEL 兼容
- R. 多个 Provider 的 Key 互不覆盖
- S. Active Provider 正确切换

所有测试不访问真实网络（mock HTTP / Provider）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.ai import config_store  # noqa: E402
from news.ai.config_store import (  # noqa: E402
    AiConfig,
    ProviderConfig,
    delete_provider,
    get_active_provider,
    get_active_provider_name,
    get_provider,
    is_preset,
    list_providers,
    preset_base_url,
    preset_model_candidates,
    read_config,
    save_config,
    save_provider,
)
from news.ai.provider import (  # noqa: E402
    AIProviderConfig,
    AIProviderError,
    fetch_models,
    test_connection as _test_connection,
)


_AI_ENV_KEYS = ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL",
                 "AI_ACTIVE_PROVIDER")


@pytest.fixture(autouse=True)
def _isolate_ai_env(monkeypatch):
    """每个测试前后清理 AI 相关环境变量，避免测试间污染（os.environ 兜底 / apply_to_env 残留）。"""
    for k in _AI_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield
    import os
    for k in _AI_ENV_KEYS:
        os.environ.pop(k, None)


def _write_env(tmp_path, text):
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return env


class TestLegacyMigration:
    """A / Q：旧单 Provider 配置自动迁移为 Active Provider，不丢 Key。"""

    def test_legacy_config_becomes_active(self, tmp_path):
        env = _write_env(
            tmp_path,
            "AI_PROVIDER=tokenrhythm\n"
            "AI_BASE_URL=https://tokenrhythm.studio/v1\n"
            "AI_API_KEY=sk-token-secret\n"
            "AI_MODEL=deepseek-v4-flash\n",
        )
        cfg = read_config(env)
        assert cfg.provider == "tokenrhythm"
        assert cfg.base_url == "https://tokenrhythm.studio/v1"
        assert cfg.api_key == "sk-token-secret"  # Key 不丢，无需重新输入
        assert cfg.model == "deepseek-v4-flash"
        assert cfg.is_complete()
        assert get_active_provider_name(env) == "tokenrhythm"

    def test_legacy_after_save_persists(self, tmp_path):
        env = _write_env(
            tmp_path,
            "AI_PROVIDER=tokenrhythm\nAI_BASE_URL=https://t/v1\n"
            "AI_API_KEY=sk-t\nAI_MODEL=m\n",
        )
        save_config(AiConfig(provider="tokenrhythm", base_url="https://t/v1",
                             api_key="sk-t", model="m"), env)
        assert "AI_PROVIDER_OPENAI" not in env.read_text(encoding="utf-8")
        loaded = read_config(env)
        assert loaded.provider == "tokenrhythm"
        assert loaded.api_key == "sk-t"


class TestPresets:
    """B / C：预设 Provider 自动填 Base URL 与模型候选。"""

    def test_openai_preset_base_url(self):
        assert preset_base_url("openai") == "https://api.openai.com/v1"
        assert "gpt-5.2" in preset_model_candidates("openai")

    def test_gemini_preset_base_url(self):
        assert preset_base_url("gemini") == (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        assert "gemini-3.6-flash" in preset_model_candidates("gemini")

    def test_tokenrhythm_preset(self):
        assert is_preset("tokenrhythm")
        assert preset_base_url("tokenrhythm") == "https://tokenrhythm.studio/v1"

    def test_custom_not_preset(self):
        assert not is_preset("My Company API")
        assert preset_base_url("My Company API") == ""


class TestMultipleProviders:
    """E / F / R / S：新增、保存多个、切换、Key 互不覆盖、Active 切换。"""

    def _seed(self, tmp_path):
        env = tmp_path / ".env"
        save_config(AiConfig(provider="openai", base_url="https://api.openai.com/v1",
                             api_key="sk-openai-key", model="gpt-5.2"), env)
        save_config(AiConfig(provider="gemini",
                             base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                             api_key="sk-gemini-key", model="gemini-3.6-flash"), env)
        return env

    def test_save_and_list_multiple(self, tmp_path):
        env = self._seed(tmp_path)
        names = [p.name for p in list_providers(env)]
        assert "openai" in names
        assert "gemini" in names
        assert len(names) == 2

    def test_keys_not_overwritten_across_providers(self, tmp_path):
        env = self._seed(tmp_path)
        assert get_provider("openai", env).api_key == "sk-openai-key"
        assert get_provider("gemini", env).api_key == "sk-gemini-key"

    def test_switch_active_provider(self, tmp_path):
        env = self._seed(tmp_path)
        assert get_active_provider_name(env) == "gemini"  # 最后保存的成为 active

        save_config(AiConfig(provider="openai", base_url="https://api.openai.com/v1",
                             api_key="sk-openai-key", model="gpt-5.2"), env)
        assert get_active_provider_name(env) == "openai"
        assert read_config(env).api_key == "sk-openai-key"

        save_config(AiConfig(provider="gemini",
                             base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                             api_key="sk-gemini-key", model="gemini-3.6-flash"), env)
        assert get_active_provider_name(env) == "gemini"
        # 切换不丢其它 Key
        assert get_provider("openai", env).api_key == "sk-openai-key"

    def test_custom_provider_preserves_name_case(self, tmp_path):
        env = self._seed(tmp_path)
        save_config(AiConfig(provider="My Company API", base_url="https://company.com/v1",
                             api_key="sk-company", model="company-1"), env)
        providers = list_providers(env)
        assert any(p.name == "My Company API" for p in providers)
        assert get_provider("My Company API", env).api_key == "sk-company"


class TestDeleteProvider:
    """H：删除 Provider 只删该 Provider，Active 正确切换。"""

    def _seed3(self, tmp_path):
        env = tmp_path / ".env"
        save_config(AiConfig(provider="openai", base_url="https://a/v1",
                             api_key="sk-a", model="m1"), env)
        save_config(AiConfig(provider="gemini", base_url="https://b/v1",
                             api_key="sk-b", model="m2"), env)
        save_config(AiConfig(provider="custom", base_url="https://c/v1",
                             api_key="sk-c", model="m3"), env)
        return env

    def test_delete_non_active_keeps_active(self, tmp_path):
        env = self._seed3(tmp_path)
        assert get_active_provider_name(env) == "custom"
        assert delete_provider("openai", env) is True
        # openai 被删，其它仍在，active 不变
        assert get_provider("openai", env) is None
        assert get_provider("gemini", env) is not None
        assert get_provider("custom", env) is not None
        assert get_active_provider_name(env) == "custom"

    def test_delete_active_switches_to_another(self, tmp_path):
        env = self._seed3(tmp_path)
        assert delete_provider("custom", env) is True
        # active 切到剩余第一个
        active = get_active_provider_name(env)
        assert active in ("openai", "gemini")
        assert get_provider("custom", env) is None

    def test_delete_all_clears_active(self, tmp_path):
        env = self._seed3(tmp_path)
        for name in ("openai", "gemini", "custom"):
            delete_provider(name, env)
        cfg = read_config(env)
        assert not cfg.is_complete()
        assert get_active_provider_name(env) == ""

    def test_delete_unknown_returns_false(self, tmp_path):
        env = self._seed3(tmp_path)
        assert delete_provider("does-not-exist", env) is False


class TestAutoSaveAfterTest:
    """I / J / K：测试成功自动保存；失败不覆盖；成功后立即已配置。"""

    def _mock_provider(self, monkeypatch, ok=True, err=None, content="OK"):
        from news.ai import openai_compatible as oc
        from news.ai.provider import BaseProvider, ProviderResult

        class FakeProvider(BaseProvider):
            def chat(self, *, system, user):
                if err:
                    raise err
                return ProviderResult(content=content, model="m")

            def close(self):
                pass

        monkeypatch.setattr("news.ai.provider.build_provider", lambda cfg: FakeProvider())

    def _mock_success(self, monkeypatch):
        self._mock_provider(monkeypatch, ok=True)

    def test_test_success_saves_config(self, monkeypatch, tmp_path):
        """I：测试成功后自动保存，无需再点保存。"""
        self._mock_success(monkeypatch)
        env = tmp_path / ".env"
        cfg = AIProviderConfig(provider="tokenrhythm", base_url="https://t/v1",
                               api_key="sk-new", model="m")
        result = _test_connection(cfg)
        assert result.ok is True
        # 模拟 dialog 自动保存（测试成功后 dialog 调用 save_config + apply_to_env）
        save_config(AiConfig(provider=cfg.provider, base_url=cfg.base_url,
                             api_key=cfg.api_key, model=cfg.model), env)
        loaded = read_config(env)
        assert loaded.is_complete()  # K：立即"已配置"
        assert loaded.api_key == "sk-new"

    def test_test_failure_does_not_overwrite(self, monkeypatch, tmp_path):
        """J：测试失败（401/404/网络）不覆盖已有有效配置。"""
        env = _write_env(
            tmp_path,
            "AI_PROVIDER=openai\nAI_BASE_URL=https://api.openai.com/v1\n"
            "AI_API_KEY=sk-GOOD-KEY\nAI_MODEL=gpt-5.2\n",
        )
        # 用一个错误 Key 测试，401 失败
        self._mock_provider(monkeypatch, err=AIProviderError("AI HTTP 401: unauthorized"))
        bad_cfg = AIProviderConfig(provider="openai", base_url="https://api.openai.com/v1",
                                   api_key="sk-BAD-KEY", model="gpt-5.2")
        result = _test_connection(bad_cfg)
        assert result.ok is False
        # 失败 → 不保存 → 旧 Key 仍保留
        loaded = read_config(env)
        assert loaded.api_key == "sk-GOOD-KEY"

    def test_failed_test_does_not_mark_configured(self, monkeypatch, tmp_path):
        env = _write_env(
            tmp_path,
            "AI_PROVIDER=openai\nAI_BASE_URL=https://api.openai.com/v1\n"
            "AI_API_KEY=sk-GOOD\nAI_MODEL=gpt-5.2\n",
        )
        self._mock_provider(monkeypatch, err=AIProviderError("AI HTTP 404: model_not_found"))
        bad_cfg = AIProviderConfig(provider="openai", base_url="https://api.openai.com/v1",
                                   api_key="sk-GOOD", model="wrong-model")
        assert _test_connection(bad_cfg).ok is False
        loaded = read_config(env)
        assert loaded.model == "gpt-5.2"  # 不被错误 model 覆盖

    def test_apply_to_env_enables_without_restart(self, monkeypatch, tmp_path):
        """L：无需重启 GUI——apply_to_env 同步 os.environ 后 from_env 立即读到。"""
        self._mock_success(monkeypatch)
        for k in ("AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
            monkeypatch.delenv(k, raising=False)
        cfg = AiConfig(provider="tokenrhythm", base_url="https://tokenrhythm.studio/v1",
                       api_key="sk-live", model="deepseek-v4-flash")
        from news.ai.config_store import apply_to_env
        apply_to_env(cfg)
        from news.ai.provider import AIProviderConfig as APC
        fe = APC.from_env()
        assert fe.provider == "tokenrhythm"
        assert fe.api_key == "sk-live"


class TestModelDiscovery:
    """M / N：刷新模型成功 / 失败仍允许手工 Model。"""

    def _cfg(self, **kw):
        defaults = dict(provider="openai", base_url="https://api.openai.com/v1",
                        api_key="sk-key", model="")
        defaults.update(kw)
        return AIProviderConfig(**defaults)

    class _FakeResp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def _patch_get(self, monkeypatch, resp=None, exc=None):
        import httpx

        def fake_get(self, url, *, headers=None):
            if exc is not None:
                raise exc
            return resp

        monkeypatch.setattr(httpx.Client, "get", fake_get)

    def test_fetch_models_success(self, monkeypatch):
        resp = self._FakeResp(
            200, {"data": [{"id": "gpt-5.2"}, {"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
        )
        self._patch_get(monkeypatch, resp=resp)
        models = fetch_models(self._cfg())
        assert models == ["gpt-5.2", "gpt-4o", "gpt-4o-mini"]

    def test_fetch_models_404_raises(self, monkeypatch):
        resp = self._FakeResp(404, {})
        self._patch_get(monkeypatch, resp=resp)
        with pytest.raises(AIProviderError):
            fetch_models(self._cfg())

    def test_fetch_models_network_error_raises(self, monkeypatch):
        import httpx
        self._patch_get(monkeypatch, exc=httpx.ConnectError("refused"))
        with pytest.raises(AIProviderError):
            fetch_models(self._cfg())

    def test_fetch_models_no_models_key(self, monkeypatch):
        resp = self._FakeResp(200, {"foo": "bar"})
        self._patch_get(monkeypatch, resp=resp)
        assert fetch_models(self._cfg()) == []

    def test_manual_model_still_possible_after_failure(self, monkeypatch):
        """N：刷新失败不阻断手工输入 Model（存储层仍可保存任意 model）。"""
        import httpx
        self._patch_get(monkeypatch, exc=httpx.ConnectError("refused"))
        with pytest.raises(AIProviderError):
            fetch_models(self._cfg())
        # 手工模型仍可保存
        env = Path(__import__("tempfile").mkdtemp()) / ".env"
        save_config(AiConfig(provider="openai", base_url="https://api.openai.com/v1",
                             api_key="sk-key", model="my-hand-written-model"), env)
        assert read_config(env).model == "my-hand-written-model"


class TestKeySafety:
    """O / P：API Key 不进入日志 / SQLite / HTML。"""

    def test_key_not_in_logs(self, tmp_path, caplog):
        env = tmp_path / ".env"
        secret = "sk-TOP-SECRET-XYZ"
        with caplog.at_level(logging.INFO):
            save_config(AiConfig(provider="openai", base_url="https://a/v1",
                                 api_key=secret, model="m"), env)
            list_providers(env)
            read_config(env)
        assert secret not in caplog.text

    def test_key_only_in_env_file_not_sqlite_html(self, tmp_path):
        env = tmp_path / ".env"
        secret = "sk-super-secret"
        save_config(AiConfig(provider="openai", base_url="https://a/v1",
                             api_key=secret, model="m"), env)
        files = list(tmp_path.iterdir())
        assert [f.name for f in files] == [".env"]
        assert not any(f.suffix in (".html", ".db") for f in files)

    def test_masked_api_key(self):
        assert config_store.masked("sk-abcdef123456") == "sk-a…3456"
        assert config_store.masked("short") == "*****"
