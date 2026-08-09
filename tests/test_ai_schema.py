"""AI schema 解析与校验测试（离线，不依赖真实 API）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.ai.schema import (  # noqa: E402
    AnalysisValidationError,
    ArticleAnalysis,
    extract_json_object,
    validate_analysis,
)

VALID_OBJ = {
    "summary_zh": "葡萄牙政府发布了新的财政预算案，包含多项税收调整。",
    "key_points": [
        "预算案于本周提交议会。",
        "政府计划降低企业所得税。",
        "欧盟将评估该预算案。",
    ],
    "topics": ["葡萄牙政治", "财政政策", "欧盟"],
    "entities": [
        {"name": "葡萄牙政府", "type": "organization"},
        {"name": "欧盟", "type": "organization"},
    ],
    "market_relevance": "medium",
    "market_relevance_reason": "财政政策变化可能影响葡萄牙债券与欧元区市场情绪，属于分析判断。",
    "language": "pt",
}


class TestExtractJsonObject:
    def test_direct_json(self):
        obj = extract_json_object('{"a": 1}')
        assert obj == {"a": 1}

    def test_fenced_json(self):
        text = '```json\n{"summary_zh": "你好"}\n```'
        obj = extract_json_object(text)
        assert obj == {"summary_zh": "你好"}

    def test_fenced_without_lang(self):
        text = '```\n{"k": [1, 2]}\n```'
        assert extract_json_object(text) == {"k": [1, 2]}

    def test_surrounding_text(self):
        text = '好的，这是结果：\n{"market_relevance": "high"}\n以上。'
        assert extract_json_object(text) == {"market_relevance": "high"}

    def test_empty_raises(self):
        with pytest.raises(AnalysisValidationError):
            extract_json_object("")

    def test_garbage_raises(self):
        with pytest.raises(AnalysisValidationError):
            extract_json_object("this is not json at all")

    def test_non_object_json_raises(self):
        with pytest.raises(AnalysisValidationError):
            extract_json_object("[1, 2, 3]")


class TestValidateAnalysis:
    def test_valid(self):
        analysis = validate_analysis(VALID_OBJ)
        assert isinstance(analysis, ArticleAnalysis)
        assert analysis.market_relevance == "medium"
        assert analysis.language == "pt"
        assert len(analysis.key_points) == 3
        assert analysis.entities[0]["type"] == "organization"

    def test_missing_summary(self):
        obj = dict(VALID_OBJ)
        del obj["summary_zh"]
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_bad_market_relevance(self):
        obj = dict(VALID_OBJ, market_relevance="extreme")
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_key_points_not_list(self):
        obj = dict(VALID_OBJ, key_points="not a list")
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_empty_key_points(self):
        obj = dict(VALID_OBJ, key_points=[])
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_bad_entity_type(self):
        obj = dict(VALID_OBJ, entities=[{"name": "X", "type": "planet"}])
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_entity_missing_name(self):
        obj = dict(VALID_OBJ, entities=[{"type": "company"}])
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_no_entities_allowed(self):
        obj = dict(VALID_OBJ, entities=[])
        analysis = validate_analysis(obj)
        assert analysis.entities == []

    def test_missing_language(self):
        obj = dict(VALID_OBJ)
        del obj["language"]
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)

    def test_topics_min(self):
        obj = dict(VALID_OBJ, topics=[])
        with pytest.raises(AnalysisValidationError):
            validate_analysis(obj)
