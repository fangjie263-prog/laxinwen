# AI Research Archive

把「与 AI 对话后生成的最终研究成果」（PDF / DOCX / HTML）自动归档并同步到 Notion。

## 目录

```text
data/research/
├── inbox/              # 你只需把文件丢进来
├── archive/            # 自动整理结果
│   └── YYYY-MM-DD/
│       └── Ticker_Company/
│           └── YYYYMMDDX_AI来源_股票代码_公司名称_研究主题.ext
├── failed/             # 无法读取 / 切割失败 / 上传失败的原文件
├── companies.json      # 可扩展的公司映射表（ticker / company_name / aliases / market）
└── research_archive.db # SHA-256 去重与状态库
```

## 用法

```bash
# 1. 只看计划（默认就是 dry-run，不移动、不上传）
python -m news research-archive

# 2. 确认无误后真正执行（移动 + 切割 + 上传 Notion）
python -m news research-archive --apply
```

## 规则摘要

- 只处理 `.pdf` `.docx` `.html` `.htm`；`.txt` `.md` `.png` 截图 / 草稿一律忽略。
- 文件名是第一优先级识别来源（AI / 代码 / 公司 / 日期），读不到才读内容，仍不确定就是 `Unknown`。
- `SHA-256` 相同即视为同一份内容，不重复归档、不重复上传。
- 单文件上限 4.5 MiB；超出后 PDF 按页切、DOCX 按段落切、HTML 按 section 切，命名 `_Part01/_Part02`。
- 处理顺序：复制归档 → 校验 → 写库 → 切割 → 上传成功 → 最后才清理 Inbox。
  任一步失败都**不删除**原文件，并记录错误原因。

## Windows 定时

复用现有 `Laxinwen-Notion-Sync` 计划任务，不新增第二套调度：

```text
news notion-sync            # 现有：同步阅读包
news research-archive --apply   # 新增：归档研究成果
```

在 `.env` 中设置 `RESEARCH_ARCHIVE_IN_SCHEDULER=1` 后，`news notion-sync` 运行时会
在同步阅读包**之前**顺带执行一次研究成果归档（同样复用 `NOTION_TOKEN` / `NOTION_ROOT_PAGE_ID`）。
