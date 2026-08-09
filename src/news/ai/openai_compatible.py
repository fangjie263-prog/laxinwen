"""OpenAI-compatible Provider —— 通过标准 /chat/completions 接口调用任意
OpenAI-compatible LLM（TokenRhythm、DeepSeek、CNB AI 网关等）。

- 使用 httpx 同步客户端，与抓取层解耦（不依赖 Fetcher）。
- 支持 base_url / api_key / model / timeout / temperature / max_tokens。
- 兼容两种响应形态：
  1. stream=false：标准 JSON 响应；
  2. stream=true：SSE data: 行（多数网关仅支持流式）。
- 规范化 token usage / cost 到 ProviderResult.usage。
"""

from __future__ import annotations

import json
import logging
from typing import Iterator, Optional

import httpx

from .provider import (
    AIProviderConfig,
    AIProviderError,
    BaseProvider,
    ProviderResult,
)

logger = logging.getLogger(__name__)

_DATA_PREFIX = "data:"


def _looks_like_json(payload: str) -> bool:
    """判断 data 负载是否已是一个完整 JSON 值（可被 json.loads 解析）。"""
    text = payload.strip()
    if not text:
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _looks_like_json_prefix(payload: str) -> bool:
    """判断 data 负载是否可能是"跨行 JSON 的前缀"（字符串未闭合 / 括号未闭合）。

    用于区分：坏 chunk（直接跳过，不污染后续事件）vs 一个跨多行 JSON 事件的开始。
    """
    text = payload.strip()
    if not text or text[0] not in "{[":
        return False
    depth = 0
    in_str = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return in_str or depth > 0


def _iter_sse_events(body: str) -> Iterator[Optional[str]]:
    """把 SSE 文本切分成事件，产出每个事件的 data 负载。

    基于 OpenAI Chat Completions SSE 的实际数据结构设计，同时兼容常见网关变体：

    - ``data: {...}`` 与 ``data:{...}``（冒号后可有/无空格）都可识别；
    - 事件之间用空行分隔（SSE 标准），也兼容无空行直接连续多条 ``data:``；
    - 单条 ``data:`` 已是完整 JSON 时立即产出；多行 ``data:``（JSON 跨行）
      则按 SSE 标准以 ``\\n`` 拼接为同一份 JSON 再产出；
    - 单个坏 chunk（非法 JSON）既不是完整 JSON、也不是跨行前缀时直接跳过，
      不会污染后续事件；
    - ``data: [DONE]`` 产出 ``None`` 表示流结束；
    - 注释行（``: ...``）与 ``event:`` / ``id:`` / ``retry:`` 字段行被忽略。

    例如：

    ::

        data: {"choices":[{"delta":{"content":"你"}}]}

        data: {"choices":[{"delta":{"content":"好"}}]}

        data: [DONE]

    会依次产出 ``{"choices":...}``、``{"choices":...}``、``None``。
    """
    pending: list[str] = []

    def _flush_pending() -> Iterator[Optional[str]]:
        nonlocal pending
        if pending:
            yield "\n".join(pending)
            pending = []

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            # 空行 = 事件边界（SSE 标准）。无论 pending 是否已作为完整 JSON
            # 提前产出，这里都安全地再 flush 一次（幂等，pending 已空则无事）。
            yield from _flush_pending()
            continue
        if line.startswith(":"):
            # SSE 注释行
            continue
        if line.startswith(_DATA_PREFIX):
            payload = line[len(_DATA_PREFIX):].strip()
            if payload == "[DONE]":
                yield from _flush_pending()
                yield None
                continue
            if pending:
                # 正在累积一个跨行 JSON 事件
                candidate = pending + [payload]
                joined = "\n".join(candidate)
                if _looks_like_json(joined):
                    yield joined
                    pending = []
                elif _looks_like_json(payload):
                    # 之前的 pending 是坏事件，当前行是独立完整 JSON：
                    # 丢弃坏事件，产出当前行，避免污染后续流。
                    pending = []
                    yield payload
                else:
                    pending.append(payload)
            elif _looks_like_json(payload):
                # 单条 data 已是完整 JSON → 立即作为一个事件产出（兼容无空行流）
                yield payload
            elif _looks_like_json_prefix(payload):
                # 可能是跨多行 JSON 的开始，先累积
                pending.append(payload)
            else:
                # 既不是完整 JSON、也不是跨行前缀 → 坏 chunk，跳过
                continue
        # 其它 SSE 字段行（event: / id: / retry:）不产生数据，忽略
    yield from _flush_pending()


def _extract_delta_content(choice: dict) -> str:
    """从 choices[].delta 中提取 content 文本。

    只接受字符串 content；null / 缺失 / 对象等一律视为"本 chunk 无内容"，
    不中断整个流，也不当作异常。
    """
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        # 宽松兼容：个别实现直接给 message（非流式 JSON 混入流式响应）
        message = choice.get("message")
        if isinstance(message, dict):
            delta = message
        else:
            return ""
    text = delta.get("content")
    if isinstance(text, str):
        return text
    return ""


class OpenAICompatibleProvider(BaseProvider):
    """基于 OpenAI /chat/completions 的 Provider。"""

    def __init__(self, config: AIProviderConfig):
        self.config = config
        self._client = httpx.Client(timeout=config.timeout)

    # ---------- 接口 ----------

    def chat(self, *, system: str, user: str) -> ProviderResult:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        payload["stream"] = True  # 兼容性最好：多数网关支持流式

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        logger.debug("AI 请求 model=%s url=%s", self.config.model, url)
        try:
            resp = self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIProviderError(f"AI 请求超时（{self.config.timeout}s）: {exc}") from exc
        except httpx.TransportError as exc:
            raise AIProviderError(f"AI 网络错误: {exc}") from exc

        if resp.status_code != 200:
            raise AIProviderError(
                f"AI HTTP {resp.status_code}: {resp.text[:300]}"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type or resp.text.strip().startswith("data:"):
            return self._parse_sse(resp.text)
        return self._parse_json(resp.text)

    def close(self) -> None:
        self._client.close()

    # ---------- 响应解析 ----------

    def _parse_json(self, body: str) -> ProviderResult:
        """解析非流式 JSON 响应。"""
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AIProviderError(f"AI 响应不是合法 JSON: {body[:300]}") from exc
        try:
            msg = data["choices"][0].get("message") or {}
            content = msg.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(f"AI 响应缺少 choices[0].message.content: {body[:300]}") from exc
        if not isinstance(content, str):
            # content 非字符串（对象/数组等）：序列化保留，避免静默丢失
            content = json.dumps(content, ensure_ascii=False)
        model = data.get("model", "") or self.config.model
        usage = data.get("usage") or {}
        return ProviderResult(content=content, model=model, usage=usage, raw=body)

    def _parse_sse(self, body: str) -> ProviderResult:
        """解析 SSE 流式响应（OpenAI Chat Completions SSE 格式）。

        关键设计（不对 TokenRhythm / 任何厂商做假设，按 OpenAI 标准数据结构）：

        - 数据事件形如 ``data: {...}``，负载为 ``{choices:[{delta:{content:...}}]}``；
        - 非 content chunk（role / finish_reason / 空 delta / usage）不参与内容拼接，
          也不应该导致整个流被判为"空"；
        - ``delta.content`` 只接受字符串；null / 缺失按"本 chunk 无内容"处理；
        - usage 可能出现在最后一个 chunk 或响应末尾，单独提取并合并；
        - ``data: [DONE]`` 表示流结束；
        - **只有整个流结束后仍然没有任何有效 content**，才报"AI 流式响应为空"。
        """
        chunks: list[str] = []
        usage: dict = {}
        model = self.config.model
        saw_data = False
        for payload in _iter_sse_events(body):
            if payload is None:  # data: [DONE] → 正常结束
                break
            saw_data = True
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                # 单个坏 chunk 不致命：跳过，继续后续 chunk
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("model"):
                model = obj["model"]
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            choices = obj.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    text = _extract_delta_content(choice)
                    if text:
                        chunks.append(text)
            elif isinstance(choices, dict):
                # 宽松兼容：choices 直接是对象而非数组
                text = _extract_delta_content(choices)
                if text:
                    chunks.append(text)
        content = "".join(chunks)
        if not saw_data:
            raise AIProviderError("AI 流式响应为空（未收到任何 SSE 数据）")
        if not content:
            raise AIProviderError("AI 流式响应为空（未解析到任何 content）")
        return ProviderResult(content=content, model=model, usage=usage, raw=body)


def extract_sse_json(text: str) -> Optional[dict]:
    """从 SSE 文本里提取最后一个含 usage 的 JSON chunk（供测试/调试）。"""
    last: Optional[dict] = None
    for payload in _iter_sse_events(text):
        if payload is None:
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last = obj
    return last
