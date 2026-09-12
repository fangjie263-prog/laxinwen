"""AI 来源识别测试（对应需求十二～十六、三十七）。"""

from __future__ import annotations

import pytest

from news.research.ai_source import (
    UNKNOWN_AI_SOURCE,
    detect_ai_source,
    detect_ai_source_from_content,
    detect_ai_source_from_filename,
    levenshtein,
)


@pytest.mark.parametrize(
    "filename",
    [
        "Claude 天齐锂业研究.pdf",
        "claude研究.pdf",
        "CLAUDE研究.pdf",
        "Cluade 天齐锂业研究.pdf",   # 需求明确要求的拼写错误
        "Sonnet 天齐锂业研究.pdf",
        "Anthropic 天齐锂业研究.pdf",
    ],
)
def test_claude_filename_variants(filename):
    assert detect_ai_source_from_filename(filename) == "Claude"


@pytest.mark.parametrize(
    "filename",
    [
        "ChatGPT NVDA研究.docx",
        "chatgpt.docx",
        "CHATGPT.docx",
        "Chat GPT 研究.docx",
        "ChatGTP 研究.docx",       # 需求明确要求的拼写错误
        "OpenAI 估值分析.docx",
    ],
)
def test_chatgpt_filename_variants(filename):
    assert detect_ai_source_from_filename(filename) == "ChatGPT"


@pytest.mark.parametrize(
    "filename",
    [
        "Gemini KAP成本曲线.html",
        "gemini.html",
        "GEMNI KAP研究.html",       # 需求明确要求的拼写错误
        "Google AI 行业研究.html",
        "GoogleAI 行业研究.html",
    ],
)
def test_gemini_filename_variants(filename):
    assert detect_ai_source_from_filename(filename) == "Gemini"


@pytest.mark.parametrize(
    "filename",
    [
        "天齐锂业深度研究.pdf",
        "锂价分析.docx",
        "GPU研究.html",
        "KAP成本曲线.html",
        "20260912 研究.pdf",
    ],
)
def test_filename_without_ai_is_none(filename):
    assert detect_ai_source_from_filename(filename) is None


def test_ambiguous_gpt_only_when_explicit_ai_semantics_in_filename():
    # 「GPT」在文件名里作为独立词且带 AI 语义 → 可识别
    assert detect_ai_source_from_filename("GPT NVDA研究.pdf") == "ChatGPT"


def test_ambiguous_gpt_not_forced_from_plain_content():
    # 正文里的普通 GPT 不能强制判断（需求十三）
    assert detect_ai_source_from_content("This paper uses a GPT model for tokenization.") is None


def test_content_detection_claude_anthropic():
    assert detect_ai_source_from_content("本报告由 Claude 生成，Anthropic 出品") == "Claude"


def test_content_detection_chatgpt_openai():
    assert detect_ai_source_from_content("Generated with OpenAI ChatGPT") == "ChatGPT"


def test_content_detection_gemini_google_ai():
    assert detect_ai_source_from_content("Google AI Gemini 输出") == "Gemini"


def test_content_detection_unknown():
    assert detect_ai_source_from_content("普通行业研究报告正文，没有任何 AI 署名") is None


def test_filename_takes_priority_over_content():
    match = detect_ai_source("Claude 研究.pdf", content_texts=["本报告由 ChatGPT 生成"])
    assert match.source == "Claude"
    assert match.matched_by == "filename"


def test_content_used_when_filename_unknown():
    match = detect_ai_source("天齐锂业深度研究.pdf", content_texts=["本报告由 Gemini 生成"])
    assert match.source == "Gemini"
    assert match.matched_by == "content"


def test_unknown_when_both_fail():
    match = detect_ai_source("天齐锂业深度研究.pdf", content_texts=["普通研究报告"])
    assert match.source == UNKNOWN_AI_SOURCE
    assert match.matched_by == "unknown"
    assert not match.known


def test_filename_match_does_not_read_content():
    """文件名命中时不应再触发内容识别（内容参数为惰性可迭代对象）。"""
    called: list[int] = []

    def generator():
        called.append(1)
        yield "ChatGPT"

    match = detect_ai_source("Claude 研究.pdf", content_texts=generator())
    assert match.source == "Claude"
    assert called == []


def test_fuzzy_match_is_bounded():
    # 距离限制内 → 命中（Claude / Gemini 的常见拼写错误只差 1）
    assert levenshtein("gemni", "gemini") == 1
    assert levenshtein("cluade", "claude") == 2
    # 超出 MAX_FUZZY_DISTANCE 后不再匹配（不无限模糊匹配）
    assert levenshtein("cxaxxe", "claude", limit=1) > 1
    assert detect_ai_source_from_filename("Cxaxxe研究.pdf") is None
    assert detect_ai_source_from_filename("研究者.pdf") is None
