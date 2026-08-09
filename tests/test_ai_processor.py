"""AI Processor 与存储集成测试（离线，Mock Provider）。

覆盖：
- JSON malformed → retry → 最终失败记录 error，不抛异常
- provider failure → 单篇失败不影响 batch
- 成功后 article_analysis 持久化
- 重复处理去重（已有成功分析则跳过，不重复调用 API）
- 不同 model/prompt_version 可并存
- --retry-failed 重新处理失败项
- 数据库唯一约束
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from news.ai.processor import ArticleProcessor, BatchStats, ProcessResult  # noqa: E402
from news.ai.provider import BaseProvider, ProviderResult  # noqa: E402
from news.ai.schema import ArticleAnalysis  # noqa: E402
from news.model import Article, utcnow  # noqa: E402
from news.storage import Storage  # noqa: E402

VALID_JSON = json.dumps(
    {
        "summary_zh": "葡萄牙政府提交新预算案，计划调整多项税收。",
        "key_points": ["预算案本周提交议会。", "政府计划降低企业所得税。"],
        "topics": ["葡萄牙政治", "财政政策"],
        "entities": [{"name": "葡萄牙政府", "type": "organization"}],
        "market_relevance": "medium",
        "market_relevance_reason": "财政政策可能影响市场情绪，属分析判断。",
        "language": "pt",
    },
    ensure_ascii=False,
)


class MockProvider(BaseProvider):
    """可控 Mock Provider。"""

    def __init__(self, responses=None, raise_error=None, calls=None):
        self.responses = list(responses or [])
        self.raise_error = raise_error
        self.calls = calls if calls is not None else []
        self.closed = False

    def chat(self, *, system, user):
        self.calls.append(user)
        if self.raise_error is not None:
            raise self.raise_error
        if self.responses:
            return ProviderResult(content=self.responses.pop(0), model="mock-model")
        return ProviderResult(content=VALID_JSON, model="mock-model")

    def close(self):
        self.closed = True


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def _article(storage, url="https://eco.sapo.pt/2026/08/08/a/", status="fetched", body="corpo"):
    art = Article(
        source_id="eco",
        source_name="ECO",
        canonical_url=url,
        title="Notícia A",
        authors=["Lusa"],
        published_at=utcnow(),
        body_text=body,
        language="pt-PT",
        status=status,
    )
    aid, _ = storage.insert_article(art, title_fp="fp")
    if status == "fetched":
        storage.update_article_body(
            aid,
            title=art.title,
            authors=art.authors,
            body_text=body,
            body_html=None,
            images=[],
            lead_image=None,
            published_at=art.published_at,
            fetched_at=utcnow(),
            language=art.language,
            status="fetched",
        )
    return aid


class TestProcessorSingle:
    def test_success_persists(self, storage):
        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider()
        proc = ArticleProcessor(storage, provider=provider)
        result = proc.process_article(art)
        assert result.ok is True
        assert result.status == "success"
        assert storage.analysis_exists(aid) is True
        rows = storage.list_analysis(article_id=aid)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "success"
        assert row["model"] == "mock-model"
        assert row["prompt_version"] == "v1"
        assert row["summary_zh"].startswith("葡萄牙政府")
        assert json.loads(row["entities_json"])[0]["type"] == "organization"
        proc.close()

    def test_malformed_json_retries_then_records_error(self, storage):
        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider(responses=["not json", "still not json", "nope"])
        proc = ArticleProcessor(storage, provider=provider, max_parse_retries=2)
        result = proc.process_article(art)
        assert result.ok is False
        assert result.status == "error"
        assert "解析失败" in result.error
        assert len(provider.calls) == 3  # 1 次原始 + 2 次重试
        # 失败记录入库
        rows = storage.list_analysis(article_id=aid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] != ""
        # Article 本身不丢失
        assert storage.get_article(aid) is not None
        proc.close()

    def test_provider_failure_records_error(self, storage):
        from news.ai.provider import AIProviderError

        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider(raise_error=AIProviderError("boom"))
        proc = ArticleProcessor(storage, provider=provider)
        result = proc.process_article(art)
        assert result.ok is False
        assert "AI 调用失败" in result.error
        assert storage.analysis_exists(aid) is False  # 没有成功分析
        proc.close()

    def test_validation_error_recorded(self, storage):
        bad = json.dumps(
            {
                "summary_zh": "x",
                "key_points": [],
                "topics": ["t"],
                "entities": [],
                "market_relevance": "extreme",
                "market_relevance_reason": "r",
                "language": "pt",
            }
        )
        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider(responses=[bad, bad, bad])
        proc = ArticleProcessor(storage, provider=provider, max_parse_retries=2)
        result = proc.process_article(art)
        assert result.ok is False
        assert "校验失败" in result.error or "market_relevance" in result.error
        proc.close()


class TestProcessorBatch:
    def test_batch_processes_all_and_isolation(self, storage):
        from news.ai.provider import AIProviderError

        a1 = _article(storage, url="https://eco.sapo.pt/2026/08/08/1/", body="corpo um")
        a2 = _article(storage, url="https://eco.sapo.pt/2026/08/08/2/", body="corpo dois")
        a3 = _article(storage, url="https://eco.sapo.pt/2026/08/08/3/", body="corpo tres")

        class FailSecond(MockProvider):
            def chat(self, *, system, user):
                self.calls.append(user)
                if "corpo dois" in user:
                    raise AIProviderError("network down")
                return ProviderResult(content=VALID_JSON, model="mock-model")

        provider = FailSecond()
        proc = ArticleProcessor(storage, provider=provider)
        stats = proc.process_batch(limit=5, source_id="eco")
        assert isinstance(stats, BatchStats)
        assert stats.total == 3
        assert stats.ok == 2
        assert stats.failed == 1
        assert len(stats.errors) == 1
        assert storage.count_analysis(status="success") == 2
        assert storage.count_analysis(status="failed") == 1
        proc.close()

    def test_duplicate_not_reprocessed(self, storage):
        a1 = _article(storage, url="https://eco.sapo.pt/2026/08/08/1/")
        a2 = _article(storage, url="https://eco.sapo.pt/2026/08/08/2/")
        provider = MockProvider()
        proc = ArticleProcessor(storage, provider=provider)
        stats1 = proc.process_batch(limit=5, source_id="eco")
        assert stats1.ok == 2
        calls_after_first = len(provider.calls)

        # 第二次：已分析的文章应被跳过，不重复调用 API
        stats2 = proc.process_batch(limit=5, source_id="eco")
        assert stats2.total == 0
        assert stats2.ok == 0
        assert len(provider.calls) == calls_after_first
        proc.close()

    def test_retry_failed_reprocesses(self, storage):
        from news.ai.provider import AIProviderError

        a1 = _article(storage, url="https://eco.sapo.pt/2026/08/08/1/")
        failing = MockProvider(raise_error=AIProviderError("down"))
        proc = ArticleProcessor(storage, provider=failing)
        stats = proc.process_batch(limit=5, source_id="eco")
        assert stats.failed == 1
        assert storage.count_analysis(status="failed") == 1
        proc.close()

        # 默认：失败项被排除（不算"未分析"）
        p2 = ArticleProcessor(storage, provider=MockProvider())
        stats2 = p2.process_batch(limit=5, source_id="eco")
        assert stats2.total == 0
        p2.close()

        # --retry-failed：重新处理失败项
        p3 = ArticleProcessor(storage, provider=MockProvider())
        stats3 = p3.process_batch(limit=5, source_id="eco", retry_failed=True)
        assert stats3.ok == 1
        assert storage.count_analysis(status="success") == 1
        p3.close()

    def test_article_id_single(self, storage):
        a1 = _article(storage, url="https://eco.sapo.pt/2026/08/08/1/")
        a2 = _article(storage, url="https://eco.sapo.pt/2026/08/08/2/")
        provider = MockProvider()
        proc = ArticleProcessor(storage, provider=provider)
        stats = proc.process_batch(article_id=a1)
        assert stats.total == 1
        assert stats.ok == 1
        assert storage.analysis_exists(a1)
        assert not storage.analysis_exists(a2)
        proc.close()


class TestDatabaseUnique:
    def test_same_key_upsert(self, storage):
        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider()
        proc = ArticleProcessor(storage, provider=provider)
        proc.process_article(art)
        proc.process_article(art)  # 同 provider/model/prompt_version → 覆盖
        rows = storage.list_analysis(article_id=aid)
        assert len(rows) == 1
        proc.close()

    def test_different_model_coexist(self, storage):
        aid = _article(storage)
        art = storage.get_article(aid)
        p1 = MockProvider()
        ArticleProcessor(storage, provider=p1).process_article(art)

        # 模拟不同 model
        class ModelB(MockProvider):
            def chat(self, *, system, user):
                r = super().chat(system=system, user=user)
                return ProviderResult(content=r.content, model="other-model")

        p2 = ModelB()
        # 直接调用存储层，验证唯一约束允许不同 model 并存
        from news.ai.schema import validate_analysis, extract_json_object

        obj = extract_json_object(VALID_JSON)
        analysis = validate_analysis(obj)
        storage.upsert_analysis(
            article_id=aid,
            provider="openai-compatible",
            model="other-model",
            prompt_version="v1",
            summary_zh=analysis.summary_zh,
            key_points=analysis.key_points,
            topics=analysis.topics,
            entities=analysis.entities,
            market_relevance=analysis.market_relevance,
            market_relevance_reason=analysis.market_relevance_reason,
            language=analysis.language,
            status="success",
        )
        rows = storage.list_analysis(article_id=aid)
        models = {r["model"] for r in rows}
        assert models == {"mock-model", "other-model"}


class TestProcessResultDataclass:
    def test_defaults(self):
        r = ProcessResult(article_id=1)
        assert r.ok is False
        assert r.status == "error"
        assert r.prompt_version == "v1"


# ---------------------------------------------------------------------------
# 第二阶段 TokenRhythm 兼容性修复：SSE/provider 失败有限重试 + 完整校验测试
# ---------------------------------------------------------------------------

class TestProviderFailureRetry:
    """SSE parser 失败 / provider 失败应可有限次重试，最终失败记录 status='failed'。"""

    def test_sse_empty_retries_then_records_error(self, storage):
        """SSE 解析失败（空响应）→ 有限重试 → 最终记录 failed。"""
        from news.ai.provider import AIProviderError

        aid = _article(storage)
        art = storage.get_article(aid)

        class AlwaysEmpty(MockProvider):
            def chat(self, *, system, user):
                self.calls.append(user)
                raise AIProviderError("AI 流式响应为空（未解析到任何 content）")

        provider = AlwaysEmpty()
        proc = ArticleProcessor(storage, provider=provider, max_parse_retries=2)
        result = proc.process_article(art)
        assert result.ok is False
        assert "AI 调用失败" in result.error
        assert len(provider.calls) == 3  # 1 次原始 + 2 次重试
        rows = storage.list_analysis(article_id=aid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] != ""
        proc.close()

    def test_provider_failure_recovers_on_retry(self, storage):
        """provider 第一次失败、第二次成功 → retry 后成功，不记录 failed。"""
        from news.ai.provider import AIProviderError

        aid = _article(storage)
        art = storage.get_article(aid)

        class Flaky(MockProvider):
            def chat(self, *, system, user):
                self.calls.append(user)
                if len(self.calls) == 1:
                    raise AIProviderError("AI 流式响应为空（未解析到任何 content）")
                return ProviderResult(content=VALID_JSON, model="mock-model")

        provider = Flaky()
        proc = ArticleProcessor(storage, provider=provider, max_parse_retries=2)
        result = proc.process_article(art)
        assert result.ok is True
        assert result.status == "success"
        assert len(provider.calls) == 2  # 1 次失败 + 1 次重试成功
        rows = storage.list_analysis(article_id=aid)
        assert rows[0]["status"] == "success"
        proc.close()

    def test_no_infinite_retry(self, storage):
        """重试次数受 max_parse_retries 限制，不会无限重试。"""
        from news.ai.provider import AIProviderError

        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider(raise_error=AIProviderError("boom"))
        proc = ArticleProcessor(storage, provider=provider, max_parse_retries=1)
        result = proc.process_article(art)
        assert result.ok is False
        assert len(provider.calls) == 2  # 1 次原始 + 1 次重试
        proc.close()


class TestFullValidation:
    """完整 AI response validation：location entity + 全部字段合法即可通过并入库。"""

    def test_full_response_with_location_entity_persists(self, storage):
        """含 location entity 的完整合法响应 → 成功入库，entities_json 含 location。"""
        full = json.dumps(
            {
                "summary_zh": "里斯本市政府发布了新的交通规划，涉及多条地铁线路建设。",
                "key_points": ["新规划涵盖三条地铁线路。", "项目预计 2027 年开工。"],
                "topics": ["城市交通", "基础设施", "里斯本"],
                "entities": [
                    {"name": "Lisbon", "type": "location"},
                    {"name": "里斯本市政府", "type": "organization"},
                    {"name": "Metropolitano de Lisboa", "type": "company"},
                ],
                "market_relevance": "medium",
                "market_relevance_reason": "基础设施投资可能影响建筑与工程板块情绪，属分析判断。",
                "language": "pt",
            },
            ensure_ascii=False,
        )
        aid = _article(storage)
        art = storage.get_article(aid)
        provider = MockProvider(responses=[full])
        proc = ArticleProcessor(storage, provider=provider)
        result = proc.process_article(art)
        assert result.ok is True
        rows = storage.list_analysis(article_id=aid)
        row = rows[0]
        assert row["status"] == "success"
        entities = json.loads(row["entities_json"])
        types = {e["type"] for e in entities}
        assert "location" in types
        assert "organization" in types
        assert "company" in types
        assert row["market_relevance"] == "medium"
        assert json.loads(row["key_points_json"])
        assert json.loads(row["topics_json"])
        proc.close()
