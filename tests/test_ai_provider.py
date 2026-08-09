"""AI Provider 解析测试（离线）：SSE / JSON 响应、网络错误、HTTP 错误。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.ai.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from news.ai.provider import (  # noqa: E402
    AIProviderConfig,
    AIProviderError,
    ProviderResult,
)

SSE_BODY = (
    'data: {"id":"1","model":"deepseek-v4-flash","choices":[{"delta":{"content":"Hel"}}],"usage":null}\n'
    'data: {"id":"1","model":"deepseek-v4-flash","choices":[{"delta":{"content":"lo"}}],"usage":null}\n'
    'data: {"id":"1","model":"deepseek-v4-flash","choices":[{"delta":{}}],'
    '"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"credit":0.01}}\n'
    "data: [DONE]\n"
)


class FakeResponse:
    def __init__(self, status=200, text="", content_type="text/event-stream"):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}


class FakeClient:
    """可控 httpx 客户端替身。"""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.posted = []

    def post(self, url, json=None, headers=None):
        self.posted.append((url, json, headers))
        if self._exc is not None:
            raise self._exc
        return self._response


def _provider(client=None, **overrides):
    cfg = AIProviderConfig(
        provider="openai-compatible",
        base_url=overrides.get("base_url", "https://example.com/v1"),
        api_key=overrides.get("api_key", "test-key"),
        model=overrides.get("model", "test-model"),
        timeout=30.0,
        temperature=0.2,
        max_tokens=100,
    )
    p = OpenAICompatibleProvider(cfg)
    p._client = client or FakeClient(FakeResponse())
    return p


class TestParseSse:
    def test_sse_content_and_usage(self):
        p = _provider(client=FakeClient(FakeResponse(text=SSE_BODY)))
        result = p.chat(system="s", user="u")
        assert isinstance(result, ProviderResult)
        assert result.content == "Hello"
        assert result.model == "deepseek-v4-flash"
        assert result.usage["total_tokens"] == 12
        assert result.usage["credit"] == 0.01

    def test_sse_http_error(self):
        p = _provider(client=FakeClient(FakeResponse(status=401, text="unauthorized")))
        with pytest.raises(AIProviderError):
            p.chat(system="s", user="u")

    def test_sse_timeout(self):
        import httpx

        p = _provider(client=FakeClient(exc=httpx.TimeoutException("slow")))
        with pytest.raises(AIProviderError):
            p.chat(system="s", user="u")

    def test_sse_empty_raises(self):
        p = _provider(client=FakeClient(FakeResponse(text="data: [DONE]\n")))
        with pytest.raises(AIProviderError):
            p.chat(system="s", user="u")

    def test_sse_multiline_json_with_extra_fields(self):
        # 兼容 reasoning_content 等额外字段
        body = (
            'data: {"choices":[{"delta":{"content":"OK","reasoning_content":"thinking..."}}],"usage":null}\n'
            'data: {"choices":[{"delta":{}}],"usage":{"total_tokens":5}}\n'
            "data: [DONE]\n"
        )
        p = _provider(client=FakeClient(FakeResponse(text=body)))
        result = p.chat(system="s", user="u")
        assert result.content == "OK"


class TestParseJson:
    def test_json_response(self):
        body = (
            '{"id":"1","model":"m1","choices":[{"message":{"content":"hi"}}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}'
        )
        p = _provider(client=FakeClient(FakeResponse(text=body, content_type="application/json")))
        result = p.chat(system="s", user="u")
        assert result.content == "hi"
        assert result.usage["total_tokens"] == 6

    def test_json_missing_content(self):
        p = _provider(client=FakeClient(FakeResponse(text='{"choices":[{"message":{}}]}')))
        with pytest.raises(AIProviderError):
            p.chat(system="s", user="u")

    def test_json_invalid(self):
        p = _provider(client=FakeClient(FakeResponse(text="<html>error</html>")))
        with pytest.raises(AIProviderError):
            p.chat(system="s", user="u")


class TestPayload:
    def test_payload_has_expected_fields(self):
        client = FakeClient(FakeResponse(text=SSE_BODY))
        p = _provider(client=client)
        p.chat(system="sys", user="usr")
        url, payload, headers = client.posted[0]
        assert url.endswith("/chat/completions")
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 100
        assert payload["stream"] is True
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert headers["Authorization"] == "Bearer test-key"

    def test_temperature_omitted_when_none(self):
        client = FakeClient(FakeResponse(text=SSE_BODY))
        cfg = AIProviderConfig(
            provider="openai-compatible",
            base_url="https://example.com/v1",
            api_key="k",
            model="m",
            temperature=None,
            max_tokens=None,
        )
        p = OpenAICompatibleProvider(cfg)
        p._client = client
        p.chat(system="s", user="u")
        _, payload, _ = client.posted[0]
        assert "temperature" not in payload
        assert "max_tokens" not in payload
