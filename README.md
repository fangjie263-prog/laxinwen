# Laxinwen

Laxinwen 是一套面向个人研究的金融新闻采集、归档与分析工具。它把多个公开新闻源统一收集到 SQLite，再通过可选的 OpenAI-compatible AI 服务生成结构化研究结果，并导出为网页、便携阅读包和 Word 文档。

```text
News Sources → Discovery → Fetch → Extraction
             → Quality / Deduplication → SQLite
             → AI Processing（可选）→ HTML / Word Research Package
```

当前代码支持 ECO、HKEJ、RFI 和 NYT Chinese 四个新闻源，提供 CLI、Windows Tkinter GUI，以及基于 Windows Task Scheduler 的多任务后台抓取。

## 功能概览

- RSS、RSSHub、栏目页和站点专用 adapter 发现文章。
- httpx 下载正文，Trafilatura 和站点专用解析逻辑提取内容。
- canonical URL 和标题指纹去重。
- SQLite 保存文章、正文、来源、发布时间和抓取状态。
- OpenAI-compatible `/chat/completions` 结构化 AI 分析。
- News Archive、AI Research HTML、独立 HTML、HTML 新闻包和 Word 研究阅读包。
- Windows GUI 管理抓取、阅读、AI 设置、导出、调度和抓取监控。
- 多个相互独立的 Windows 定时任务。

## 环境要求与安装

- Python 3.12（仓库的 `.python-version` 为 `3.12`）。
- [uv](https://docs.astral.sh/uv/)。
- Windows GUI 和 Task Scheduler 需要 Windows；CLI、数据库和离线测试也可在其它支持 Python 3.12 的系统运行。

Windows PowerShell：

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装项目：

```bash
git clone <repo-url>
cd laxinwen
uv sync --extra dev
uv run news --help
```

快速开始：

```bash
uv run news fetch
uv run news fetch --site eco
uv run news fetch --site hkej
uv run news fetch --site rfi
uv run news fetch --site nytchinese
uv run news list --limit 30
uv run news status
uv run news process --site eco --limit 3
uv run news export --format news-html --site eco --limit 50
```

Windows GUI：

```powershell
uv run news gui
```

也可以双击项目根目录的 `NewsReader.bat`；需要保留控制台日志时使用 `NewsReader-Console.bat`。

## 新闻来源

站点配置位于 `sites/`，每个 YAML 文件对应一个来源。

| ID | 来源 | 实际发现方式 |
| --- | --- | --- |
| `eco` | ECO – Economia Online | 官方 RSS、栏目页和 load-more；通用发现流程 |
| `hkej` | HKEJ 信報財經新聞 | HKEJ 专用 source adapter；列表分页和正文解析 |
| `rfi` | RFI 法广中文 | RFI 专用 source adapter；RSS / RSSHub fallback、HTML fallback 和质量过滤 |

网络可达性、站点响应和页面结构可能影响结果。程序会记录单站点或单文章错误，并尽量继续处理其它来源或文章。

## 抓取与数据模型

通用来源按配置尝试官方 RSS、RSSHub、栏目页，并在需要时使用 load-more 补齐候选。ECO 的 load-more 参数由 `sites/eco.yaml` 配置；HKEJ 和 RFI 使用 `src/news/sources/` 中的专用 adapter。

`fetch --limit N` 的目标是获得最多 N 篇可读新闻，而不是简单截取 N 个候选 URL。已存在文章、下载失败、正文为空或质量不合格的候选不会消耗可读数量；候选耗尽时程序会正常结束并报告实际可读数量。因此“没有新文章”不等于“抓取失败”。

普通来源使用 httpx 下载，再由 Trafilatura 和站点配置提取正文。RSS 已携带完整正文时可以跳过重复下载。RFI 对过短正文和节目/播音类标题执行专用 `low_quality` 过滤。

去重分为两层：

1. `canonical_url` 唯一索引。
2. 结合来源标题后缀规则的标题指纹。

默认数据库为 `data/news.db`。主要表：

- `articles`：来源、标题、作者、发布时间、发现/抓取时间、正文、图片、语言和状态。
- `article_analysis`：Provider、模型、Prompt 版本、摘要、关键观点、主题、实体、市场相关性、语言、状态、错误和 usage。

`article_analysis` 以 `(article_id, provider, model, prompt_version)` 唯一，因此同一文章可保留不同分析版本。数据库时间保存为 UTC ISO 8601，网页和 Word 转换为 Asia/Shanghai。

## AI Processing

`news fetch` 负责采集和入库，`news process` 选择已成功抓取且尚未分析的文章，调用 Provider，校验结构化 JSON，再写入 `article_analysis`。AI 失败不会删除文章，可用 `--retry-failed` 重试。

调用接口：

```text
POST {AI_BASE_URL}/chat/completions
```

配置变量：

| 变量 | 含义 |
| --- | --- |
| `AI_PROVIDER` | Provider 标识，默认 `openai-compatible` |
| `AI_BASE_URL` | API Base URL |
| `AI_API_KEY` | API Key |
| `AI_MODEL` | 模型标识 |
| `AI_TIMEOUT` | 超时秒数，默认 60 |
| `AI_TEMPERATURE` | 温度，默认 0.2 |
| `AI_MAX_TOKENS` | 最大输出 token，默认 4000 |

CNB 流水线支持 `CNB_TOKEN` 回退到 CNB AI 网关。GUI 支持保存和切换多个 Provider；预设包括 OpenAI、Gemini 和 TokenRhythm，模型可通过 `/models` 动态获取或手工输入。

项目根目录 `.env` 示例：

```dotenv
AI_PROVIDER=openai-compatible
AI_BASE_URL=https://api.example.com/v1
AI_API_KEY=replace-with-your-key
AI_MODEL=replace-with-your-model
```

单次覆盖：

```bash
uv run news process --site eco --limit 3 \
  --ai-provider openai-compatible \
  --ai-base-url https://api.example.com/v1 \
  --ai-model replace-with-your-model
```

AI 输出字段为 `summary_zh`、`key_points`、`topics`、`entities`、`market_relevance`、`market_relevance_reason` 和 `language`；若服务端提供，还保存 token usage / cost。

## HTML 与 Word 导出

### News Archive 与 AI Research

News Archive 直接读取 `articles` 表，不要求 AI 成功，并区分已分析、AI 失败和未分析文章：

```bash
uv run news export --format news-html --site eco --limit 100
```

默认输出到 `data/export/news-html/<site>/`。

AI Research HTML 只展示成功的 AI 分析：

```bash
uv run news export --format html
uv run news export --format html --site eco
uv run news export --format html --article-id 1
```

默认输出到 `data/export/html/`。

### Portable HTML + Word

GUI 的“导出便携阅读包（HTML + Word）”是面向用户的单一入口，一次生成同一批文章的 Portable HTML 和 Word DOCX。两者使用相同的数据库文章、source、limit、排序和过滤逻辑。

CLI 执行同一组合：

```bash
uv run news export --type both --site eco --limit 100
```

便携阅读包结构：

```text
data/export/portable/Laxinwen-<SITE>-<date>-<time>-<job>/
├── index.html
├── articles/
├── server.py
├── Open-Reader.bat
└── Laxinwen-<SITE>-<date>-<time>-<job>.docx
```

双击 `Open-Reader.bat` 后，内置服务器只监听 `127.0.0.1`，通过 HTTP 打开页面而非 `file://`；整个目录可以复制到其它电脑。

Word 使用真正的内部 bookmark / hyperlink：

- 目录标题点击后跳转到对应正文 bookmark。
- 每篇正文的“↑ 返回目录”跳回目录 bookmark。
- 原文 URL 是可点击的外部超链接，会在浏览器打开原网页。
- 每篇正文保留标题、来源、发布时间、正文内容和原文链接。

高级 CLI 能力：也可以单独导出 Word，但这不是 GUI 的普通用户流程：

```bash
uv run news export --format word --site eco --limit 100
```

其它高级 CLI 格式：

```bash
uv run news export --format jsonl
uv run news export --format markdown
uv run news export --format portable --site eco --limit 100
uv run news export --format package --site eco --limit 100
```

- `portable`：单个 self-contained HTML。
- `package`：`index.html + articles/` HTML 新闻包。
- `reader`：带本地 HTTP 服务器、Windows 启动器和同批 Word 的便携阅读包。GUI 的“导出便携阅读包（HTML + Word）”使用这一类便携阅读包能力，一次生成 HTML + Word。

默认输出位置与当前 CLI 实现一致：JSONL / Markdown 使用 `NEWS_EXPORTS`（未设置时为 `exports/`）；HTML 使用 `data/export/html/`；News Archive 使用 `data/export/news-html/<site>/`；Portable HTML、HTML 新闻包和便携阅读包使用 `data/export/portable/`；单独的 Word 文件使用 `data/export/word/`。`--output` 可覆盖相应输出路径。

## Windows GUI

```powershell
uv run news gui
uv run news gui --site eco
uv run news gui --site hkej
uv run news gui --site rfi
uv run news gui --site all
```

GUI 实际包含：

- 来源选择：ECO、HKEJ、RFI 或全部。
- 抓取数量：默认 100，支持正整数和 50/100/200 快捷按钮。
- 抓取最新新闻、打开新闻库、AI 分析、AI 设置、打开 AI 研究结果。
- 单一导出入口：一次生成 Portable HTML + Word。
- 自动抓取 / 定时任务：管理多个独立任务。
- 抓取监控、运行日志、数据库和 AI 状态。

监控区区分 `FETCH`（发现、下载、提取、质量检查、入库）和 `EXPORT`（HTML / Word 导出）。导出失败不会被归类为抓取失败。GUI 关闭时它启动的本地 HTTP 服务器会停止。

## Windows Task Scheduler

Windows Task Scheduler 是真正的调度器；Laxinwen 不让 Python 常驻等待时间。GUI 只是配置和状态工具。

计划任务会启动：

```text
python -m news scheduled-fetch --job-id <id>
```

任务执行“抓取 → 去重 → 入库 → HTML + Word 导出”，完成后 Python 进程退出。关闭 GUI 不会阻止已安装任务继续运行。

每个任务可独立配置 `id`、`name`、`source`、`frequency`、`time`、`interval_hours`、`limit` 和 `enabled`。来源为 `rfi`、`eco` 或 `hkej`；频率为 `daily` 或 `hourly`，每小时间隔可为 1、2、3 或 6。任务名格式为 `Laxinwen-<SOURCE>-<job_id>`，同一 job 重复安装会更新原任务。

### scheduler.json

运行时配置为 `data/scheduler.json`：

```json
{
  "jobs": [
    {
      "id": "rfi-morning",
      "name": "RFI 每日早报",
      "enabled": true,
      "source": "rfi",
      "frequency": "daily",
      "time": "08:00",
      "interval_hours": 1,
      "limit": 50,
      "auto_export": true,
      "export_type": "portable"
    }
  ]
}
```

`time` 用于每日任务，`interval_hours` 用于每小时任务。自动导出运行逻辑固定生成 Portable HTML + Word；`export_type` 是保留的兼容字段。旧版单任务扁平结构会在读取时转换为 `jobs` 列表。

### CLI 与 BAT

```bash
uv run news scheduled-fetch --job-id rfi-morning
uv run news scheduler install rfi-morning
uv run news scheduler delete rfi-morning
uv run news scheduler run rfi-morning
uv run news scheduler status rfi-morning
```

```text
scripts/windows/install-scheduled-fetch.bat [job_id]
scripts/windows/delete-scheduled-fetch.bat [job_id]
scripts/windows/run-scheduled-fetch-now.bat [job_id]
```

后台日志为 `data/logs/scheduled-fetch.log`，分别记录 `FETCH: SUCCESS/FAILED` 和 `EXPORT: SUCCESS/FAILED/SKIPPED`。没有新文章、候选耗尽或数据库已包含全部候选，不自动等同于失败。

## CLI 参考

以下参数对应当前 `src/news/cli.py` 的 argparse 定义。

```text
news [--version] <command>

news fetch [--site SITE] [--limit N] [--timeout S]
           [--retries N] [--interval S] [--retry-failed]
           [--db PATH] [-v]

news list [--source SOURCE] [--limit N] [--db PATH] [-v]
news status [--source SOURCE] [--db PATH] [-v]
news gui [--site SITE] [--db PATH] [-v]
news serve [--export-root DIR] [--db PATH] [-v]

news process [--site SITE] [--limit N] [--article-id ID]
             [--retry-failed] [--ai-provider NAME]
             [--ai-base-url URL] [--ai-api-key KEY]
             [--ai-model MODEL] [--db PATH] [-v]

news scheduled-fetch [--job-id ID] [--source SOURCE] [--limit N]
                     [--config PATH] [--log-file PATH]
                     [--db PATH] [-v]

news scheduler {install,delete,run,status} [JOB_ID]
                [--config PATH] [--project-root DIR]
                [--db PATH] [-v]

news export [--format FORMAT] [--type TYPE] [--site SITE]
            [--source SOURCE] [--article-id ID] [--limit N]
            [--job-id ID] [--output PATH] [--db PATH] [-v]
```

`--format` 合法值：`jsonl`、`markdown`、`html`、`news-html`、`portable`、`package`、`reader`、`word`。
`--type` 合法值：`portable`、`word`、`both`。未指定 `--format` 或 `--type` 会返回错误。

## 路径与安全

| 路径 / 变量 | 用途 |
| --- | --- |
| `data/news.db` | SQLite 数据库 |
| `data/export/` | HTML、News Archive、便携阅读包 |
| `data/logs/scheduled-fetch.log` | 定时任务日志 |
| `data/scheduler.json` | Windows 多任务配置 |
| `NEWS_DB` | 覆盖 CLI 默认数据库路径 |
| `NEWS_EXPORTS` | 覆盖 JSONL / Markdown 默认导出目录 |
| `NEWS_SITES_DIR` | 覆盖站点 YAML 目录 |

- 不要把真实 API Key 写入代码、YAML、BAT、README 或 Git。
- `.env`、`data/`、`exports/`、虚拟环境和测试缓存已由 `.gitignore` 排除。
- API Key 不写入 SQLite、HTML、Word、日志或 `scheduler.json`。
- 抓取只面向有权访问的公开内容，不要绕过登录、验证码、付费墙或访问控制。
- 本地阅读服务器只监听 `127.0.0.1`。

## 项目结构

```text
laxinwen/
├── pyproject.toml
├── uv.lock
├── .python-version
├── .gitignore
├── README.md
├── NewsReader.bat
├── NewsReader-Console.bat
├── sites/{eco.yaml,hkej.yaml,rfi.yaml}
├── scripts/windows/{install-scheduled-fetch.bat,
│                    delete-scheduled-fetch.bat,
│                    run-scheduled-fetch-now.bat,
│                    install-notion-sync.bat,
│                    run-notion-sync-now.bat,
│                    delete-notion-sync.bat}
├── src/news/
│   ├── cli.py, config.py, model.py, normalize.py
│   ├── discover.py, fetch.py, extract.py, pipeline.py
│   ├── storage.py, export.py, news_archive.py, html_export.py
│   ├── portable.py, word_export.py, reader_server.py
│   ├── gui.py, ai_settings_dialog.py, beijing.py
│   ├── scheduled_fetch.py, scheduler_config.py, task_scheduler.py
│   ├── run_identity.py, notion_sync.py
│   ├── sources/{base.py,hkej.py,rfi.py}
│   └── ai/{config_store.py,provider.py,openai_compatible.py,
│           processor.py,prompts.py,schema.py}
└── tests/
```

## 测试

```bash
uv run python -m pytest
uv run python -m pytest -m network
```

GUI 测试需要 Tk 显示环境；Linux 无头环境可在已安装 Xvfb 时运行：

```bash
xvfb-run -a uv run python -m pytest tests/test_gui.py tests/test_gui_monitor.py tests/test_gui_scheduler_status.py
```

测试不会自动创建真实 Windows Task Scheduler 任务；真实任务行为需要在 Windows 上由用户显式安装或运行。

## Notion 自动归档

`news notion-sync` 是独立的归档层，只扫描已经生成的 Portable Reader 包和可选的 ResearchReader 输出，不重新抓取新闻、不重新运行 AI，也不修改 HTML 或 Word 内容。默认扫描 `data/export/portable/`，Notion 页面结构为 Source → Date → Run → Artifact：

```text
NOTION_ROOT_PAGE_ID
├── ECO
│   └── 2026-08-24
│       └── 10:00:00 · eco-default
├── RFI
│   └── 2026-08-24
│       └── 08:00:00 · rfi-default
└── HKEJ
    └── 2026-08-24
        └── 09:00:00 · hkej-default
```

新的运行会在日期页下创建独立运行页，例如 `08:00:12 · rfi-default`；`origin` 只保存在同步状态中，不创建 `ResearchReader` 顶层页面。ResearchReader 通过 `RESEARCHREADER_OUTPUT_ROOT` 和 `RESEARCHREADER_BOOKS_ROOT` 配置，直接归档 `daily.html`、中文/双语 HTML 以及原始 EPUB/PDF，不上传 `images/` 或中间文件。

Run 页面保存归档索引：文章数量、任务标识、手机 HTML、完整 HTML 阅读包 ZIP 和 Word 阅读包；不会把每篇新闻拆成 Notion 页面或大量 block。Laxinwen 的手机 HTML 与完整目录 ZIP 分开上传，ZIP 排除 DOCX，Word 作为独立 DOCX 上传。ResearchReader 的 HTML 直接上传，不生成 ZIP。文件使用 Notion 官方 File Upload API；大文件按 API 要求分片上传。

### 配置

在项目根目录 `.env` 中设置：

```dotenv
NOTION_TOKEN=
NOTION_ROOT_PAGE_ID=
NOTION_MAX_UPLOAD_MB=4.5
RESEARCHREADER_OUTPUT_ROOT=
RESEARCHREADER_BOOKS_ROOT=
```

需要先在 Notion 创建 Integration，将它授权给 `Laxinwen News` 根页面，并填写根页面 ID。Token 不会写入代码、日志或 `data/notion-sync.json`。

`NOTION_MAX_UPLOAD_MB` 可选，默认安全值为 4.5 MiB。手机 HTML、ResearchReader HTML、完整 HTML ZIP 和 DOCX 均使用该限制；HTML 超限时按文章/section 切分为多个 HTML，不改成 ZIP。完整 HTML ZIP 会排除 Portable 目录中的所有 `.docx`，因此 Word 不会在 ZIP 中重复上传。ZIP 超限时按文件重新装箱为多个标准 ZIP；单个源文件仍超限时，会生成有恢复 manifest 的分片。Word 小于阈值时直接上传 DOCX，超过阈值时使用同样的分片和恢复说明机制。

`RESEARCHREADER_OUTPUT_ROOT` 指向 ResearchReader 的 HTML 输出目录，`RESEARCHREADER_BOOKS_ROOT` 指向 EPUB/PDF 原始书籍目录；两项都配置时才会扫描 ResearchReader。路径只从 `.env` 或系统环境变量读取，不会写入代码。

### CLI 与状态

```powershell
uv run news notion-sync
uv run news notion-sync --dry-run
```

同步状态保存在 `data/notion-sync.json`。新状态 identity 使用 origin、source、date、run_id、artifact_type、artifact_variant 和 part；每个 artifact 记录 fingerprint、文件大小和 Notion upload ID，因此失败重试只补传缺失 part。旧 package/artifact key 和旧 Date Page ID 继续兼容，已同步的历史包不会重新上传。全部 artifact 和归档 block 成功后才标记包完成；同一个 run 重复扫描会跳过，不会重复创建 Run Page、上传文件或添加归档 block。同步期间会锁定状态文件，避免手工运行与 Scheduler 并发写入。

同步输出区分 `SYNC SUCCESS`、`SYNC SKIP` 和 `SYNC FAILED`。文件上传或 Notion API 在中途失败时，已完成的页面和文件 ID 会保存在状态文件，下一次运行可以继续。

### Windows 独立任务

Notion 同步不依赖新闻抓取任务，也不要求 GUI 或 PowerShell 窗口持续打开。可使用以下脚本安装每小时运行一次的独立任务：

```text
scripts/windows/install-notion-sync.bat
scripts/windows/run-notion-sync-now.bat
scripts/windows/delete-notion-sync.bat
```

真实 Notion API 上传需要有效的 Token、根页面授权和网络连接；没有这些条件时只能运行 `--dry-run` 或离线测试，不能宣称完成真实 Notion 验证。

## License

本项目当前尚未声明开源许可证。在仓库添加正式 `LICENSE` 文件之前，请不要将本项目按 MIT、Apache-2.0 或其它开源许可证重新发布。
