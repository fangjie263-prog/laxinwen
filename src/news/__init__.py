"""laxinwen —— 个人金融新闻采集与研究数据库。

数据流：
新闻网站 → RSS/RSSHub/栏目页 → 文章 URL → 下载 → 正文提取
→ 统一 Article 模型 → 去重 → SQLite → Markdown/JSONL 导出
→ 未来 AI 摘要 / 翻译 / 分类 / 研究分析
"""

__version__ = "0.1.0"
