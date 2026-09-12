"""AI 来源识别（文件名优先 → 文件内容 → Unknown）。

设计原则（严格对齐需求十二～十六）：

- **只有两层**：文件名（第一优先级）→ 文件内容（第二优先级）→ ``Unknown``；
- 优先精确匹配 → 别名 → 常见拼写错误 → **有限** Levenshtein 距离 → Unknown；
- 不确定就 ``Unknown``，绝不猜；
- ``GPT`` 这类有歧义的短词只在**文件名**中出现明确 AI 语义时才算命中，
  且要求独立词边界，避免 ``GPT`` 命中普通正文里的无关缩写。

本模块是纯函数层：内容识别所需的文本由调用方（``document_text``）提供，
便于测试直接喂字符串。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 正式 AI 来源取值：只有这三个 + Unknown（需求十四）
UNKNOWN_AI_SOURCE = "Unknown"
AI_SOURCES = ("Claude", "ChatGPT", "Gemini")

# 有限拼写距离：超过该值不再接受（避免误判）
MAX_FUZZY_DISTANCE = 1

# 规范名 → 精确别名（大小写归一化后比较）
_EXACT_ALIASES: dict[str, tuple[str, ...]] = {
    "Claude": (
        "claude", "claudeai", "anthropic", "anthropicclaude", "sonnet",
        "claude sonnet", "claude opus", "claude haiku",
        "cluade", "clade", "claud", "cloude",
    ),
    # 注意：别名按「Claude → ChatGPT → Gemini」顺序匹配，因此把「不可能是
    # Claude 的短词」放在后面，避免抢走判断。
    "ChatGPT": (
        "chatgpt", "chat gpt", "chatgptopenai", "openai", "openai chatgpt",
        "chatgtp", "chatgbt", "chatgp",
        "chatgpt4", "chatgpt5", "gpt4", "gpt4o", "gpt5", "gpt-4", "gpt-5",
        # 独立词「GPT」：只在文件名里出现且带 AI 语义时命中（_AMBIGUOUS 控制）
        "gpt",
    ),
    "Gemini": (
        "gemini", "googlegemini", "google ai", "googleai", "bard",
        "geminipro", "geminiadvanced", "deepmind", "google gemini",
        "gemni", "gemimi", "gemnini",
    ),
}

# 常见拼写错误（有限、可控、来自需求十三/十四的例子）
_COMMON_TYPOS: dict[str, str] = {
    "cluade": "Claude",
    "clade": "Claude",
    "claud": "Claude",
    "cloude": "Claude",
    "chatgtp": "ChatGPT",
    "chatgbt": "ChatGPT",
    "chatgtp4": "ChatGPT",
    "chatgp": "ChatGPT",
    "gemni": "Gemini",
    "gemimi": "Gemini",
    "gemimni": "Gemini",
    "gemnini": "Gemini",
}

# 歧义短词（可能出现在普通正文里）：只允许「文件名精确独立词」命中。
# 注意：
#   - ``gpt`` 只在文件名里作为独立词时命中（需求十三）；
#   - ``clade`` / ``gemn i`` 这类是「孤立拼写错误」，只有在紧随 AI 上下文
#     （如 ``clade研究``）时才接受，避免英文正文里的普通单词误判。
_AMBIGUOUS = {"gpt", "ai", "bard", "o1", "o3", "clade", "gnem", "gnemi"}

# 内容识别关键词：要求独立词边界，避免 "AI" 之类误命中
_CONTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Claude": ("claude", "anthropic"),
    "ChatGPT": ("chatgpt", "chat gpt", "openai"),
    "Gemini": ("gemini", "google gemini", "google ai", "googleai"),
}

_SOURCE_ORDER = ("Claude", "ChatGPT", "Gemini")


@dataclass(frozen=True)
class AiSourceMatch:
    """AI 来源识别结果。"""

    source: str
    matched_by: str  # "filename" / "content" / "unknown"
    evidence: str = ""

    @property
    def known(self) -> bool:
        return self.source != UNKNOWN_AI_SOURCE


def _fold(value: str) -> str:
    """大小写 / 空格 / 全角归一化：用于所有匹配前的预处理。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\s_\-–—·、,，.。;；:：/\\|+*~()\[\]{}<>«»\"'“”‘’]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(value: str) -> str:
    return _fold(value).replace(" ", "")


def _tokenize(value: str) -> list[str]:
    return [token for token in _fold(value).split(" ") if token]


def levenshtein(a: str, b: str, *, limit: int = MAX_FUZZY_DISTANCE) -> int:
    """带早停的编辑距离；超过 ``limit`` 直接返回 ``limit + 1``。"""
    a = str(a or "")
    b = str(b or "")
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _match_alias_text(text: str, *, allow_ambiguous: bool) -> Optional[tuple[str, str]]:
    """在已归一化文本里匹配别名；返回 ``(规范名, 命中词)``。"""
    folded = _fold(text)
    compact = _compact(text)
    if not folded:
        return None

    # 1) 精确别名（含多词别名）
    for source in _SOURCE_ORDER:
        for alias in _EXACT_ALIASES[source]:
            alias_folded = _fold(alias)
            if not alias_folded:
                continue
            if " " in alias_folded:
                if f" {alias_folded} " in f" {folded} ":
                    return source, alias
            elif alias_folded in _AMBIGUOUS:
                if allow_ambiguous and alias_folded in _tokenize(text):
                    return source, alias
            elif alias_folded in compact or alias_folded in _tokenize(text):
                return source, alias

    # 2) 常见拼写错误：整体 token 或 token 边界匹配
    tokens = _tokenize(text)
    for token in tokens:
        if token in _COMMON_TYPOS:
            return _COMMON_TYPOS[token], token
    compact_typos = {
        typo: source for typo, source in _COMMON_TYPOS.items()
        if len(typo) >= 5 and typo.isascii()
    }
    for typo, source in sorted(compact_typos.items(), key=lambda item: -len(item[0])):
        if " " in typo:
            continue
        if typo in compact or re.search(rf"(?<![a-z0-9]){re.escape(typo)}(?![a-z0-9])", folded):
            return source, typo

    # 3) 有限 Levenshtein（只对不含空格的 token，且长度足够，避免短词误判）
    for token in tokens:
        if len(token) < 5 or token in _AMBIGUOUS:
            continue
        for source in _SOURCE_ORDER:
            for alias in _EXACT_ALIASES[source]:
                alias_folded = _fold(alias)
                if " " in alias_folded or len(alias_folded) < 5:
                    continue
                if levenshtein(token, alias_folded) <= MAX_FUZZY_DISTANCE:
                    return source, f"{token}~{alias}"
    return None


def detect_ai_source_from_filename(filename: str) -> Optional[str]:
    """第一优先级：从文件名识别 AI 来源；无法确定返回 ``None``。"""
    match = _match_alias_text(filename, allow_ambiguous=True)
    return match[0] if match else None


def detect_ai_source_from_content(text: str) -> Optional[str]:
    """第二优先级：从文件内容识别；无法确定返回 ``None``。

    内容识别比文件名保守：``GPT`` / ``AI`` 这类歧义词一律不参与，
    只接受 ``Claude`` / ``Anthropic`` / ``ChatGPT`` / ``OpenAI`` /
    ``Gemini`` / ``Google AI`` 等明确 AI 语义。
    """
    if not text:
        return None
    folded = _fold(text[:200_000])
    if not folded:
        return None
    for source in _SOURCE_ORDER:
        for keyword in _CONTENT_KEYWORDS[source]:
            keyword_folded = _fold(keyword)
            if " " in keyword_folded:
                if f" {keyword_folded} " in f" {folded} ":
                    return source
            elif re.search(rf"(?<![a-z0-9]){re.escape(keyword_folded)}(?![a-z0-9])", folded):
                return source
    # 内容层也接受有限拼写错误，但要求独立词
    for token in set(_tokenize(text[:200_000])):
        if token in _COMMON_TYPOS:
            return _COMMON_TYPOS[token]
    return None


def detect_ai_source(
    filename: str,
    *,
    content_texts: Iterable[str] = (),
    read_content: bool = True,
) -> AiSourceMatch:
    """完整的两级识别流程。

    1. 文件名命中 → 直接返回（不再读内容，符合「文件名第一优先级」）；
    2. 文件名未命中且 ``read_content`` → 依次读取内容文本；
    3. 仍未命中 → ``Unknown``（不猜）。
    """
    from_filename = detect_ai_source_from_filename(filename)
    if from_filename:
        return AiSourceMatch(from_filename, "filename", filename)
    if read_content:
        for index, text in enumerate(content_texts):
            from_content = detect_ai_source_from_content(text)
            if from_content:
                return AiSourceMatch(from_content, "content", f"input#{index}")
    return AiSourceMatch(UNKNOWN_AI_SOURCE, "unknown", "")
