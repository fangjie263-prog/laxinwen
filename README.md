# laxinwen

个人金融新闻采集与研究数据库（Personal financial news collection & research database）。

> **核心目标**：不重新发明新闻爬虫，而是组合成熟开源组件，把多个新闻网站的公开新闻稳定采集、统一保存，
> 未来接入 AI 做摘要、翻译、分类、公司/行业识别和投资影响分析。

```
新闻网站
    ↓
RSS / RSSHub / 必要时栏目页
    ↓
发现文章 URL
    ↓
下载网页 (httpx)
    ↓
正文提取 (Trafilatura)
    ↓
统一 Article 数据模型
    ↓
去重（URL + 标题指纹）
    ↓
SQLite
    ↓
Markdown / JSONL 导出
    ↓
AI Processing Layer（第二阶段）
    ↓
结构化新闻分析 → SQLite (article_analysis)
```

---

## 设计原则

1. **复用成熟组件**：`RSSHub`（如有需要）、`feedparser`（RSS 解析）、`httpx`（HTTP）、
   `Trafilatura`（正文提取）、`selectolax`（栏目页解析）、`SQLite`（标准库 `sqlite3`）。
2. **不过度设计**：第一版就是普通 Python 项目 + SQLite，不用 Kafka/Redis/Celery/ORM/向量库/微服务。
3. **抓取与 AI 解耦**：第一阶段只做 `发现 → 下载 → 提取 → 去重 → 入库 → 导出`；
   AI 层通过统一 `Article` 模型在未来接入，不写死在抓取代码里。

---

## 环境要求

- **Python 3.12**（由 `uv` 自动管理，无需系统预装）
- **[uv](https://docs.astral.sh/uv/)** —— Python 包与虚拟环境管理器

```bash
# 安装 uv（如未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

---

## 安装与运行

```bash
# 克隆仓库
git clone <repo-url> laxinwen
cd laxinwen

# 安装依赖（自动创建 .venv，Python 3.12）
uv sync --extra dev

# ---------- 抓取 ----------
# 抓取全部已配置站点
uv run news fetch

# 只抓取 ECO（葡萄牙财经媒体）
uv run news fetch --site eco

# 控制每站候选文章数 / 超时 / 重试 / 同域间隔
uv run news fetch --site eco --limit 30 --timeout 20 --retries 3 --interval 2

# 抓取后重试失败文章
uv run news fetch --site eco --retry-failed

# ---------- 查看 ----------
uv run news list                 # 列出最近新闻
uv run news list --source eco    # 按站点过滤
uv run news status               # 数据库与抓取状态

# ---------- 导出 ----------
uv run news export --format jsonl
uv run news export --format markdown
uv run news export --format jsonl --source eco --output /path/to/exports

# ---------- 测试 ----------
uv run python -m pytest          # 全部离线测试
uv run python -m pytest -m network   # 需要外网的在线测试
```

---

## CLI 命令一览

| 命令 | 说明 |
| --- | --- |
| `news fetch [--site <id>] [--limit N] [--timeout S] [--retries N] [--interval S] [--retry-failed]` | 抓取新闻 |
| `news list [--source <id>] [--limit N]` | 列出最近新闻 |
| `news status [--source <id>]` | 显示数据库与抓取状态（含 AI 分析统计） |
| `news process [--site <id>] [--limit N] [--article-id <id>] [--retry-failed]` | AI 处理已入库文章（生成结构化分析） |
| `news export --format jsonl\|markdown [--source <id>] [--output DIR]` | 导出 |

环境变量（可选）：

- `NEWS_SITES_DIR`：站点配置目录（默认 `./sites`）
- `NEWS_DB`：SQLite 数据库路径（默认 `./data/news.db`）
- `NEWS_EXPORTS`：导出目录（默认 `./exports`）
- AI 相关变量见下方 [AI Processing Layer](#ai-processing-layer) 一节

---

## 项目结构

```
laxinwen/
├── pyproject.toml          # uv 项目定义、依赖、CLI 入口
├── README.md
├── .gitignore
├── sites/                  # 站点配置（一个网站一个 YAML）
│   ├── eco.yaml            # ECO – Economia Online（已跑通）
│   └── hkej.yaml           # HKEJ 信报（预留，见“已知问题”）
├── src/news/
│   ├── __init__.py
│   ├── cli.py              # 命令行入口（fetch / list / status / process / export）
│   ├── config.py           # 站点配置加载
│   ├── model.py            # 统一 Article 数据模型
│   ├── normalize.py        # URL 规范化 + 标题指纹
│   ├── storage.py          # SQLite 存储层（articles + article_analysis）
│   ├── discover.py         # 新闻发现（RSS → RSSHub → 栏目页）
│   ├── fetch.py            # 下载层（httpx，Fetcher 抽象）
│   ├── extract.py          # 正文提取（Trafilatura）
│   ├── pipeline.py         # 抓取 pipeline（串联各阶段）
│   ├── export.py           # JSONL / Markdown 导出
│   └── ai/                 # AI Processing Layer（第二阶段）
│       ├── __init__.py
│       ├── provider.py         # Provider 抽象与配置（环境变量 / .env）
│       ├── openai_compatible.py# OpenAI-compatible Provider（httpx）
│       ├── prompts.py          # 版本化 Prompt（事实优先，PROMPT_VERSION）
│       ├── schema.py           # JSON 解析与 schema 校验
│       └── processor.py        # Article → AI → 校验 → 入库编排
├── tests/                  # 离线单元测试 + 在线冒烟测试
├── data/                   # SQLite 数据库（运行时生成）
└── exports/                # 导出文件（运行时生成）
```

---

## 新闻发现机制

按以下优先级（在 `sites/<id>.yaml` 中描述）：

1. **官方 RSS / Atom**（`rss:`）—— 第一优先；
2. **RSSHub**（`rsshub:`）—— 没有官方 RSS 时检查已有 Route；
3. **公开栏目页**（`lists:`）—— 前两者都不适用时才用 selectolax 解析；
4. 站内搜索 —— 第一阶段不实现。

> 增加一个“有官方 RSS 的简单网站”：在 `sites/` 下新增一个 YAML 即可，
> **无需修改核心 Python 代码**。

示例 `sites/eco.yaml`（节选）：

```yaml
id: eco
name: ECO – Economia Online
language: pt-PT
rss: https://eco.sapo.pt/feed/
lists:
  - url: https://eco.sapo.pt/ultimas/
    link_selector: "article.card-list a.card__title, article.card--list a.link-cover"
    article_url_pattern: "https://eco\\.sapo\\.pt/(?:[0-9]{4}/[0-9]{2}/[0-9]{2}/|entrevista/|descodificador/|...)/[^/]+/$"
extract:
  favor_recall: true
  clean_patterns:
    - "Escolha o ECO como fonte preferida no Google"
    - "Assine o ECO Premium.*"
title_suffixes:
  - " – ECO"
```

---

## 正文提取

- 默认路径：`httpx → Trafilatura`。
- 站点级参数：`extract:` 可覆盖 Trafilatura 参数、正文杂讯清理 `clean_patterns`。
- 只有必须 JS 渲染时才使用 Playwright（第一阶段 ECO 不需要，因此未启用；
  代码中 `BaseFetcher` 抽象已为 `PlaywrightFetcher` 预留）。

---

## 去重（两层）

1. **canonical URL 去重**：移除 fragment、`utm_*`/`fbclid`/`gclid` 等追踪参数、域名大小写归一、
   去掉默认端口；SQLite 对 `canonical_url` 建 `UNIQUE INDEX`。
2. **标题指纹**：NFKC 归一 → 去站点后缀 → 小写 → 折叠空白 → 去标点 → SHA-256，
   同源内比对。

> 不做跨站点的“同一新闻故事”语义去重：Reuters 与 BBC 报道同一事件，两篇都应保留。

---

## 数据库

- 第一版唯一事实来源：**SQLite**（标准库 `sqlite3`，无 ORM）。
- JSONL / Markdown 都是**派生文件**，随时可从 SQLite 重新导出。
- 文章表字段：`id / source_id / source_name / canonical_url / title / authors /
  published_at / discovered_at / fetched_at / body_text / body_html / images /
  lead_image / language / status / title_fp`。
- 第二阶段新增 `article_analysis` 表，结构与唯一约束见 [AI Processing Layer — 数据库结构](#数据库结构-1)。

---

## AI Processing Layer

第二阶段新增能力：把已经入库的 `Article` 交给一个 **OpenAI-compatible LLM API**，
生成**结构化新闻分析**（中文摘要、关键事实、主题、实体、市场相关度等），并持久化回 SQLite。

```
Article
   ↓
AI Provider (OpenAI-compatible)
   ↓
Structured Analysis (严格 JSON)
   ↓
SQLite (article_analysis)
```

抓取层与 AI 层**完全解耦**：`news fetch` 负责抓新闻，`news process` 负责处理已入库新闻，
两者可独立运行。单篇 AI 失败不影响其它文章，也不会让 Article 丢失。

### 1. 环境变量配置

AI 配置全部通过环境变量（或项目根 `.env` 文件）提供，**API Key 绝不进入代码 / YAML / Git**。

```bash
# 通用 OpenAI-compatible Provider（TokenRhythm 示例）
export AI_PROVIDER=tokenrhythm
export AI_BASE_URL=https://tokenrhythm.studio/v1
export AI_API_KEY=your-key
export AI_MODEL=deepseek-v4-flash

# 可选
export AI_TIMEOUT=60          # 请求超时（秒），默认 60
export AI_TEMPERATURE=0.2     # 采样温度，默认 0.2
export AI_MAX_TOKENS=4000     # 输出最大 token，默认 4000
```

也可以使用项目根 `.env`（已被 `.gitignore` 排除，不会进入 Git）：

```bash
# .env
AI_PROVIDER=tokenrhythm
AI_BASE_URL=https://tokenrhythm.studio/v1
AI_API_KEY=your-key
AI_MODEL=deepseek-v4-flash
```

> **CNB 流水线内免配置**：在 CNB 流水线环境中运行 `news process` 时，
> 若未设置 `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL`，系统会自动回退到
> CNB AI 网关（`https://api.cnb.cool/<repo>/-/ai` + `CNB_TOKEN`），
> 无需额外配置即可真实调用（见“真实验收”）。

### 2. 安装

与第一阶段相同：

```bash
uv sync --extra dev
```

AI 层只依赖项目已有的 `httpx`，未新增任何第三方依赖。

### 3. process 命令

```bash
uv run news process --site eco --limit 3
```

行为：

1. 从 SQLite 找出**已成功抓取但还没有 AI analysis** 的 ECO 文章；
2. 逐篇调用 AI Provider；
3. 解析并校验严格 JSON（失败自动有限重试，仍失败则记录 `status='failed'`）；
4. 保存到 `article_analysis`；
5. 单篇失败不影响其它文章。

### 4. 单篇处理

```bash
uv run news process --article-id 12
```

只处理指定文章 ID（已分析过则跳过）。

### 5. 批量处理

```bash
# 全站未分析文章，默认最多 5 篇（成本控制）
uv run news process --limit 5

# 指定站点
uv run news process --site eco --limit 10
```

### 6. retry failed

```bash
uv run news process --site eco --retry-failed --limit 5
```

重新处理之前 AI 处理失败的记录（`article_analysis.status='failed'`）。

### 7. 切换 OpenAI-compatible Provider

不写死任何厂商/模型，只通过环境变量切换：

```bash
# 切换到 DeepSeek 官方
export AI_BASE_URL=https://api.deepseek.com/v1
export AI_MODEL=deepseek-chat
export AI_API_KEY=your-deepseek-key
uv run news process --site eco --limit 3

# 切换到任意 OpenAI-compatible endpoint
export AI_BASE_URL=https://your-gateway.example/v1
export AI_MODEL=your-model
export AI_API_KEY=your-key
uv run news process --site eco --limit 3
```

也可通过 CLI 临时覆盖（Key 仍来自环境 / `.env`）：

```bash
uv run news process --site eco --limit 3 \
  --ai-provider deepseek --ai-base-url https://api.deepseek.com/v1 \
  --ai-model deepseek-chat
```

Provider 名只作为数据库标签保存，不影响调用逻辑。

### 8. 数据库结构

新增表 `article_analysis`（SQLite）：

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `article_id` | 外键 → `articles.id`（ON DELETE CASCADE） |
| `provider` | Provider 标识（如 `openai-compatible`） |
| `model` | 模型名 |
| `prompt_version` | Prompt 版本（当前 `v1`） |
| `summary_zh` | 中文摘要 |
| `key_points_json` | 关键事实（JSON 数组） |
| `topics_json` | 主题标签（JSON 数组） |
| `entities_json` | 实体列表（JSON 数组） |
| `market_relevance` | `high` / `medium` / `low` |
| `market_relevance_reason` | 理由（1~3 句） |
| `language` | 原文语言代码（如 `pt` / `en` / `zh`） |
| `status` | `success` / `failed` |
| `error` | 失败原因 |
| `usage_json` | token usage / cost（API 返回时保存） |
| `created_at` / `updated_at` | 时间戳 |

**唯一约束**：`UNIQUE(article_id, provider, model, prompt_version)`。

设计选择：同一篇文章未来可以用**不同模型 / 不同 Prompt 版本**重新分析并共存
（方便比较 v1 / v2 / v3 的输出），相同参数的重复分析则覆盖更新，不会产生重复记录。

### 9. 测试方法

```bash
# 全部离线测试（不依赖任何真实 API）
uv run python -m pytest

# 仅 AI 相关测试
uv run python -m pytest tests/test_ai_schema.py tests/test_ai_provider.py tests/test_ai_processor.py
```

离线测试使用 Mock Provider，覆盖：JSON 解析、schema 校验、malformed JSON、重试、
Provider 失败、数据库持久化、重复分析去重、批量处理、单篇失败不中断 batch。

真实验收（可选，需要 API 可用）：

```bash
uv run news fetch --site eco          # 确保数据库有 ECO 文章
uv run news process --site eco --limit 3   # 真实调用 API
uv run news status                     # 查看 AI 分析统计
```

### 10. AI 输出字段

每篇 Article 生成以下结构化字段：

1. `summary_zh` — 中文摘要（3~5 自然段以内，准确描述事实，不添加原文没有的信息）；
2. `key_points` — 3~5 条关键事实；
3. `topics` — 主题标签（如 `葡萄牙政治`、`财政政策`、`欧盟`）；
4. `entities` — 实体列表，区分 `company / person / organization / country / location / product`；
5. `market_relevance` — 仅判断“是否可能具有金融市场研究价值”（`high/medium/low`），**不给投资建议**；
6. `market_relevance_reason` — 1~3 句理由；
7. `language` — 原文语言代码。

Prompt 强制要求**事实优先**：事实必须来自文章，不虚构，不确定必须标记，
`market_relevance` 是分析判断而非事实，不允许把“可能影响”写成“已经影响”。

---

## 错误处理与抓取礼仪

- 单站失败不影响其它站点；单篇失败不影响其它文章（记录 `status='failed'` 并可重试）。
- 抓取礼仪：合理 User-Agent、超时、重试、同域名请求间隔、尊重 `Retry-After`、
  不高频并发、不绕过登录/验证码/付费墙，只抓有权访问的公开内容。

---

## 已知问题与下一步

- **HKEJ（信报）**：从当前运行环境（欧洲数据中心）无法建立 TCP 连接（超时），
  属于网络可达性限制而非代码问题。已预留 `sites/hkej.yaml` 配置。
  第一阶段硬性验收以 **ECO** 为准（见下）。
- **AI 层已知限制**：`market_relevance` 是模型的分析判断而非事实；
  当前只生成最小结构化字段，投资建议 / 预测 / RAG / 向量库 / Agent 等均未实现
  （见 `NEXT_PHASE.md`）。
- 下一步建议（详细见 `NEXT_PHASE.md`）：
  1. 增加更多财经站点（Reuters/FT/WSJ 等）——每个站点新增一个 YAML；
  2. 为必须 JS 的站点实现 `PlaywrightFetcher`；
  3. 接入 RSSHub 公共实例或自建实例；
  4. 用 `cron` 定时调度 `news fetch` + `news process`；
  5. Prompt v2：多模型对比 / 历史分析比较 / 投资研究标签。

---

## 第一阶段：ECO 端到端验收结果

真实运行验证（`uv run news fetch --site eco`）：

| 项目 | 结果 |
| --- | --- |
| 发现来源 | 官方 RSS `https://eco.sapo.pt/feed/`（含正文）；栏目页备选已验证 |
| 文章发现 | 15 篇（本次运行） |
| 成功下载 | 15 / 15 |
| 成功提取正文 | 15 / 15 |
| 失败 | 0 |
| 去重 | 二次运行 15 篇全部跳过（URL 去重） |
| SQLite 入库 | 15 篇（`status=fetched`） |

> 运行环境差异可能导致每次抓取的数量略有不同（以 RSS 当时条目为准）。

---

## 第二阶段：AI Processing Layer 验收结果

真实运行验证（CNB AI 网关 + `deepseek-v4-flash`，详见 PR 报告）：

| 项目 | 结果 |
| --- | --- |
| 离线测试 | 84 项全部通过（含第一阶段 46 项） |
| Provider | OpenAI-compatible（CNB AI 网关，`AI_PROVIDER=openai-compatible`） |
| 模型 | `deepseek-v4-flash` |
| 处理文章 | 20 篇 ECO（分 4 批：3+3+11+3） |
| AI 成功 | 20 / 20 |
| AI 失败 | 0 |
| 二次运行 | 全部跳过（0 篇，不重复调用 API） |
| 数据库 | `article_analysis` 成功写入 20 条 |
| token usage | prompt 30,429 / completion 12,791 / total 43,220 |
| credit/cost | 1.45（CNB AI 网关返回的 credit） |
| retry-failed | 失败项默认排除；`--retry-failed` 真实重试 3 篇 3/3 成功 |

> 实际数字以本次 PR 的验收运行输出为准。
