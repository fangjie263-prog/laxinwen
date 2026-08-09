"""AI Processing Layer —— 把已入库的 Article 交给 OpenAI-compatible LLM，
生成结构化新闻分析结果并持久化回 SQLite。

数据流：
    Article
      ↓
    AI Provider (OpenAI-compatible)
      ↓
    Structured Analysis
      ↓
    SQLite (article_analysis)

与抓取层完全解耦：本包只消费已入库的 Article，不修改任何抓取逻辑。
"""

from .openai_compatible import OpenAICompatibleProvider
from .processor import ArticleProcessor, BatchStats, ProcessResult
from .prompts import PROMPT_VERSION
from .provider import AIProviderConfig, AIProviderError, BaseProvider, ProviderResult, build_provider, load_dotenv
from .schema import AnalysisValidationError, extract_json_object, validate_analysis

__all__ = [
    "AIProviderConfig",
    "AIProviderError",
    "BaseProvider",
    "ProviderResult",
    "OpenAICompatibleProvider",
    "build_provider",
    "load_dotenv",
    "ArticleProcessor",
    "BatchStats",
    "ProcessResult",
    "PROMPT_VERSION",
    "AnalysisValidationError",
    "extract_json_object",
    "validate_analysis",
]
