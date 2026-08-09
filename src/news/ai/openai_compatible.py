"""OpenAI-compatible Provider —— 通过标准 /chat/completions 接口调用任意
OpenAI-compatible LLM（TokenRhythm、DeepSeek、CNB AI 网关等）。

- 使用 httpx 同步客户端，与抓取层解耦（不依赖 Fetcher）。
- 支持 base_url / api_key / model / timeout / temperature / max_tokens。
- 兼容两种响应形态：
  1. stream=false：标准 JSON 响应；
  2. stream=true：SSE data: 行（CNB AI 网关等仅支持流式）。
- 规范化 token usage / cost 到 ProviderResult.usage。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from .provider import (
    AIProviderConfig,
    AIProviderError,
    BaseProvider,
    ProviderResult,
)

logger = logging.getLogger(__name__)

_DONE_MARK = "data: [DONE]"
_DATA_PREFIX = "data: "


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
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(f"AI 响应缺少 choices[0].message.content: {body[:300]}") from exc
        model = data.get("model", "") or self.config.model
        usage = data.get("usage") or {}
        return ProviderResult(content=content or "", model=model, usage=usage, raw=body)

    def _parse_sse(self, body: str) -> ProviderResult:
        """解析 SSE 流式响应（data: {...} 行，可能夹杂空行 / 注释）。"""
        chunks: list[str] = []
        usage: dict = {}
        model = self.config.model
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line == _DONE_MARK or line.startswith("data: [DONE]"):
                break
            if not line.startswith(_DATA_PREFIX):
                # 某些实现直接给 JSON，容错处理
                payload = line
            else:
                payload = line[len(_DATA_PREFIX):].strip()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("model"):
                model = obj["model"]
            usage = obj.get("usage") or usage
            choices = obj.get("choices") or []
            for choice in choices:
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    chunks.append(text)
        content = "".join(chunks)
        if not content:
            raise AIProviderError("AI 流式响应为空（未解析到任何 content）")
        return ProviderResult(content=content, model=model, usage=usage, raw=body)


def extract_sse_json(text: str) -> Optional[dict]:
    """从 SSE 文本里提取最后一个含 usage 的 JSON chunk（供测试/调试）。"""
    last: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(_DATA_PREFIX) or line.startswith(_DONE_MARK):
            continue
        try:
            obj = json.loads(line[len(_DATA_PREFIX):])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last = obj
    return last
