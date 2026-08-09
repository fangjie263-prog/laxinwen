"""AI 输出解析与 Schema 校验。

- 要求模型返回严格 JSON；此处兜底处理模型常见的 Markdown 包裹
  （```json ... ``` / ``` ... ```）。
- 校验所有必需字段、类型与取值，不满足则抛 AnalysisValidationError。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .prompts import VALID_ENTITY_TYPES, VALID_MARKET_RELEVANCE

# 至少需要多少条实体/要点/主题
MIN_KEY_POINTS = 1
MIN_TOPICS = 1
MIN_ENTITIES = 0  # 允许无实体（文章可能不涉及明确实体）


class AnalysisValidationError(ValueError):
    """分析结果 schema 校验失败。"""


@dataclass
class ArticleAnalysis:
    """一篇 Article 的结构化 AI 分析结果（纯数据，不含 DB 字段）。"""

    summary_zh: str
    key_points: list[str]
    topics: list[str]
    entities: list[dict[str, str]]
    market_relevance: str
    market_relevance_reason: str
    language: str


def extract_json_object(text: str) -> dict:
    """从模型输出中提取 JSON 对象。

    处理步骤：
    1. 直接尝试 json.loads；
    2. 若被 ```json ... ``` 包裹，剥离代码块；
    3. 若首尾有多余文字，尝试截取第一个 { 到最后一个 }。
    """
    text = (text or "").strip()
    if not text:
        raise AnalysisValidationError("模型输出为空")

    # 剥离 ```json ... ``` 代码块
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise AnalysisValidationError(f"JSON 顶层不是对象: {type(obj).__name__}")
    except json.JSONDecodeError:
        pass

    # 尝试截取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise AnalysisValidationError("无法从模型输出中解析出 JSON 对象")


def _as_str_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise AnalysisValidationError(f"{field_name} 必须是数组")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AnalysisValidationError(f"{field_name} 元素必须是非空字符串")
        result.append(item.strip())
    return result


def validate_analysis(obj: Any) -> ArticleAnalysis:
    """校验分析 dict，返回 ArticleAnalysis。"""
    if not isinstance(obj, dict):
        raise AnalysisValidationError("分析结果必须是 JSON 对象")

    def _req_str(name: str) -> str:
        value = obj.get(name)
        if not isinstance(value, str) or not value.strip():
            raise AnalysisValidationError(f"缺少或无效字段: {name}")
        return value.strip()

    summary_zh = _req_str("summary_zh")
    market_relevance = _req_str("market_relevance")
    market_relevance_reason = _req_str("market_relevance_reason")
    language = _req_str("language")

    if market_relevance not in VALID_MARKET_RELEVANCE:
        raise AnalysisValidationError(
            f"market_relevance 取值非法: {market_relevance!r}（应为 {'/'.join(VALID_MARKET_RELEVANCE)}）"
        )

    key_points = _as_str_list(obj.get("key_points"), "key_points")
    if len(key_points) < MIN_KEY_POINTS:
        raise AnalysisValidationError(f"key_points 至少需要 {MIN_KEY_POINTS} 条")
    if len(key_points) > 8:
        key_points = key_points[:8]

    topics = _as_str_list(obj.get("topics"), "topics")
    if len(topics) < MIN_TOPICS:
        raise AnalysisValidationError(f"topics 至少需要 {MIN_TOPICS} 个")

    entities_raw = obj.get("entities")
    if not isinstance(entities_raw, list):
        raise AnalysisValidationError("entities 必须是数组")
    entities: list[dict[str, str]] = []
    for ent in entities_raw:
        if not isinstance(ent, dict):
            raise AnalysisValidationError("entities 元素必须是对象")
        name = ent.get("name")
        etype = ent.get("type")
        if not isinstance(name, str) or not name.strip():
            raise AnalysisValidationError("entities 元素缺少非空 name")
        if etype not in VALID_ENTITY_TYPES:
            raise AnalysisValidationError(
                f"entities.type 取值非法: {etype!r}（应为 {'/'.join(VALID_ENTITY_TYPES)}）"
            )
        entities.append({"name": name.strip(), "type": etype})

    return ArticleAnalysis(
        summary_zh=summary_zh,
        key_points=key_points,
        topics=topics,
        entities=entities,
        market_relevance=market_relevance,
        market_relevance_reason=market_relevance_reason,
        language=language,
    )
