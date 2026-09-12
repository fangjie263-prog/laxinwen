"""AI Research Archive —— 把「与不同 AI 对话后生成的研究成果」归档进 Laxinwen。

定位（严格对齐现有架构）：

- 本包是 Laxinwen 的一个**模块**，不是独立项目：扫描 → 识别 → 去重 → 归档
  → 切割 → 复用现有 ``NotionClient`` 上传；
- 不新建第二套 Notion Token / Client / Scheduler；
- 不新增 GUI，入口只有 CLI（``news research-archive``）与现有
  ``Laxinwen-Notion-Sync`` 计划任务；
- 默认 **dry-run**：只扫描 / 只识别 / 只输出计划，不移动、不重命名、不上传。

处理范围只有最终研究成果：``.pdf`` / ``.docx`` / ``.doc`` / ``.html`` / ``.htm``。
``.txt`` / ``.md`` / ``.png`` / 截图 / 草稿等一律跳过。

识别原则（顺序无关）：文件名里的 date / ticker / AI / company / 序号各自独立
扫描后再组合，不依赖固定顺序的正则整串匹配。
"""

from .config import ResearchArchiveConfig, load_research_config
from .scanner import ResearchArchiveScanner, ResearchCandidate
from .pipeline import ResearchArchiveResult, run_research_archive

__all__ = [
    "ResearchArchiveConfig",
    "load_research_config",
    "ResearchArchiveScanner",
    "ResearchCandidate",
    "ResearchArchiveResult",
    "run_research_archive",
]
