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


# ---------------------------------------------------------------------------
# SSE parser 回归测试（第二阶段 TokenRhythm 兼容性修复）
# 使用固定 fixture 模拟真实 OpenAI-compatible SSE 格式，不依赖真实 API。
# ---------------------------------------------------------------------------

from tests.sse_fixtures import (  # noqa: E402,F401
    SSE_DONE_ONLY,
    SSE_GARBAGE,
    SSE_LATE_CONTENT,
    SSE_MALFORMED_MIDDLE,
    SSE_MULTI_CONTENT,
    SSE_NO_CONTENT,
    SSE_NO_SPACE_NO_BLANK,
    SSE_STANDARD,
    SSE_USAGE_LAST,
    SSE_WITH_COMMENTS,
)


class TestSseRegression:
    """针对真实 TokenRhythm SSE 解析问题的回归测试。"""

    def test_standard_sse_concatenates_content(self):
        """Case A：多个正常 content chunks → 正确拼接。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_STANDARD)))
        result = p.chat(system="s", user="u")
        assert result.content == "你好"
        assert result.model == "deepseek-v4-flash"
        assert result.usage["total_tokens"] == 98

    def test_multi_content_chunks(self):
        """Case A：无空行分隔的多个 content chunks 拼接。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_MULTI_CONTENT)))
        result = p.chat(system="s", user="u")
        assert result.content == "Hello world!"

    def test_late_content_not_treated_as_empty(self):
        """Case B：前几个 chunk 没有 content，后续有 content → 成功。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_LATE_CONTENT)))
        result = p.chat(system="s", user="u")
        assert result.content == "成功内容"

    def test_done_marker(self):
        """Case C：data: [DONE] 正常结束。"""
        # 有正常 content 后遇到 [DONE] 正常收尾
        p = _provider(client=FakeClient(FakeResponse(text=SSE_MULTI_CONTENT)))
        result = p.chat(system="s", user="u")
        assert result.content == "Hello world!"

    def test_http200_no_content_fails(self):
        """Case D：HTTP 200 但整个 SSE 没有 content → 正确失败。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_NO_CONTENT)))
        with pytest.raises(AIProviderError, match="流式响应为空"):
            p.chat(system="s", user="u")

    def test_usage_in_last_chunk(self):
        """Case E：最后一个 chunk 包含 usage → 正确解析 usage。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_USAGE_LAST)))
        result = p.chat(system="s", user="u")
        assert result.content == "分析完成"
        assert result.usage["prompt_tokens"] == 120
        assert result.usage["completion_tokens"] == 30
        assert result.usage["total_tokens"] == 150
        assert result.usage["credit"] == 0.0123

    def test_no_space_after_data_colon(self):
        """兼容 data:{...}（无空格）的网关变体。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_NO_SPACE_NO_BLANK)))
        result = p.chat(system="s", user="u")
        assert result.content == "Hi there"

    def test_comments_and_event_field_ignored(self):
        """注释行 / event: 字段不影响解析。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_WITH_COMMENTS)))
        result = p.chat(system="s", user="u")
        assert result.content == "你好"

    def test_malformed_chunk_in_middle_is_skipped(self):
        """malformed SSE：中间坏 chunk 跳过，不整体失败。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_MALFORMED_MIDDLE)))
        result = p.chat(system="s", user="u")
        assert result.content == "good result"

    def test_garbage_body_fails(self):
        """malformed SSE：完全不是 SSE → 正确失败。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_GARBAGE)))
        with pytest.raises(AIProviderError, match="流式响应为空"):
            p.chat(system="s", user="u")

    def test_done_only_no_data(self):
        """只有 data: [DONE]，没有任何 data 事件 → 正确失败。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_DONE_ONLY)))
        with pytest.raises(AIProviderError, match="流式响应为空"):
            p.chat(system="s", user="u")

    def test_empty_body_fails(self):
        """空 body → 正确失败。"""
        p = _provider(client=FakeClient(FakeResponse(text="")))
        with pytest.raises(AIProviderError, match="流式响应为空"):
            p.chat(system="s", user="u")

    def test_usage_in_same_chunk_as_content(self):
        """usage 与 content 在同一 chunk（部分实现把 usage 放在最后一条带内容的事件）。"""
        body = (
            'data: {"choices":[{"delta":{"content":"Done"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n'
            "data: [DONE]\n"
        )
        p = _provider(client=FakeClient(FakeResponse(text=body)))
        result = p.chat(system="s", user="u")
        assert result.content == "Done"
        assert result.usage["total_tokens"] == 4

    def test_choices_as_dict(self):
        """宽松兼容：choices 直接是对象而非数组。"""
        body = (
            'data: {"choices":{"delta":{"content":"Hi"}}}\n'
            "data: [DONE]\n"
        )
        p = _provider(client=FakeClient(FakeResponse(text=body)))
        result = p.chat(system="s", user="u")
        assert result.content == "Hi"

    def test_content_null_chunks_ignored(self):
        """delta.content 为 null 的 chunk 不参与拼接，也不失败。"""
        body = (
            'data: {"choices":[{"delta":{"content":null}}]}\n'
            'data: {"choices":[{"delta":{"content":"Hi"}}]}\n'
            "data: [DONE]\n"
        )
        p = _provider(client=FakeClient(FakeResponse(text=body)))
        result = p.chat(system="s", user="u")
        assert result.content == "Hi"

    def test_model_from_stream(self):
        """模型名从流式 chunk 中提取。"""
        p = _provider(client=FakeClient(FakeResponse(text=SSE_STANDARD)))
        result = p.chat(system="s", user="u")
        assert result.model == "deepseek-v4-flash"
