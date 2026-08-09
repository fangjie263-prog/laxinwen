# laxinwen

个人使用的“金融新闻采集与研究数据库”。本项目**复用成熟开源组件**，把多个新闻网站的公开内容稳定采集、统一入库，未来再接入 AI 做摘要、翻译、分类与研究分析。

**第一阶段 MVP 目标**：把一条数据流完整跑通：

```
新闻网站
  → RSS / RSSHub / 栏目页
  → 发现文章 URL
  → 下载网页 (httpx)
  → 正文提取 (Trafilatura)
  → 统一 Article 模型
  → 去重 (canonical URL + 标题指纹)
  → SQLite
  → Markdown / JSONL 导出
```

> 当前已通过 **ECO – Economia Online**（https://eco.sapo.pt/ultimas/ ）的完整端到端验收。

---

## 环境要求

- Python 3.12（由 `uv` 自动引导，无需系统预装）
- [uv](https://docs.astral.sh/uv/) 包管理器

## 快速开始

```bash
# 1. 安装依赖（自动下载 Python 3.12）
uv sync

# 2. 查看支持的网站
uv run news sites

# 3. 抓取 ECO 新闻（发现 → 下载 → 提取 → 入库）
uv run news fetch --site eco

# 4. 查看数据库中的新闻
uv run news list

# 5. 导出 JSONL（每行一篇，适合 AI / jq / 批量分析）
uv run news export --format jsonl

# 6. 导出 Markdown（按 YYYY/MM/ 目录组织，适合人阅读）
uv run news export --format markdown

# 7. 查看数据库状态
uv run news status
```

**注意**：`news` 与 `laxinwen` 是同一个 CLI 的两个别名，可互换使用。

## CLI 命令

| 命令 | 说明 |
| --- | --- |
| `news sites` | 列出所有已配置的新闻网站 |
| `news fetch --site <id> [--limit N]` | 抓取指定网站（`--limit` 限制下载篇数，用于快速验收） |
| `news list [--site <id>] [--limit N]` | 列出最近入库的文章 |
| `news export --format jsonl|markdown [--site <id>]` | 从 SQLite 导出派生文件 |
| `news status` | 显示数据库与抓取状态 |

## 项目结构

```
laxinwen/
├── pyproject.toml          # 依赖与 CLI 入口（uv 管理）
├── README.md
├── .gitignore
├── sites/
│   └── eco.yaml            # 网站配置：RSS / 栏目页 / 提取参数
├── src/laxinwen/
│   ├── cli.py              # Typer CLI
│   ├── config.py           # 网站配置加载（YAML）
│   ├── model.py            # 统一 Article 数据模型
│   ├── normalize.py        # URL 规范化 + 标题指纹
│   ├── fetch.py            # HTTP 抓取（UA / timeout / retry / 限速）
│   ├── discover.py         # 新闻发现：RSS (feedparser) / HTML 栏目页 (selectolax)
│   ├── extract.py          # 正文提取（Trafilatura + 站点级清理）
│   ├── pipeline.py         # 端到端流水线编排
│   ├── storage.py          # SQLite 存储（唯一事实来源）
│   └── exporters.py        # JSONL / Markdown 导出
├── tests/                  # 单元测试（pytest）
├── data/                   # SQLite 数据库（git 忽略）
└── exports/                # 导出文件（git 忽略）
```

## 网站配置（增加新网站）

新增一个“有官方 RSS”的网站只需**一个 YAML 文件**，无需修改 Python 代码：

```yaml
# sites/example.yaml
id: example
name: Example News
rss: https://example.com/feed/
language: en
```

对于没有 RSS、需要解析栏目页的网站，可配置 `lists`：

```yaml
id: eco
name: ECO – Economia Online
url: https://eco.sapo.pt/
language: pt-PT

rss: https://eco.sapo.pt/feed/          # 官方 RSS（第一优先）

lists:
  - url: https://eco.sapo.pt/ultimas/   # 栏目页（RSS 失败时的兜底）
    type: html
    link_selector: "article.card-list a h3.card__title"
    article_url_pattern: "https://eco\\.sapo\\.pt/20\\d{2}/\\d{2}/\\d{2}/[^/]+/"
    max_items: 50

extract:
  body_selectors:                        # Trafilatura 效果不佳时的兜底
    - ".entry__content"
  exclude_phrases:                       # 从正文中剔除的页面杂质
    - "Escolha o ECO como fonte preferida no Google"
```

### 新闻发现优先级

1. **官方 RSS / Atom**（`rss:` 字段，feedparser 解析）
2. **RSSHub**（`rsshub:` 字段，若该站有现成 Route）
3. **公开栏目页**（`lists:` 字段，selectolax 提取链接 + URL 正则过滤）
4. 站内搜索（第一阶段**不实现**）

## 设计要点

### 复用优先

| 能力 | 使用的成熟组件 | 说明 |
| --- | --- | --- |
| RSS/Atom 解析 | `feedparser` | 不自写 RSS 解析器 |
| HTTP 请求 | `httpx` | 设置 UA / timeout / retry / 域名限速 |
| 正文提取 | `trafilatura` | 不自写 HTML 正文解析器 |
| HTML 栏目页解析 | `selectolax` | 提取文章链接 |
| 浏览器渲染 | （预留 `Fetcher` 抽象） | 仅当页面必须 JS 渲染时才启用 Playwright |
| 数据库 | SQLite（stdlib `sqlite3`） | 第一版唯一事实来源，不用 ORM |

### Article 统一数据模型

所有来源统一转换为 `Article`，是“抓取层”与未来“AI 层”之间的数据契约，至少包含：`source_id / source_name / canonical_url / title / authors / published_at / discovered_at / fetched_at / body_text / body_html / images / lead_image / language / status`。

- `canonical_url` 用于 URL 去重（去掉 fragment、`utm_*`、`fbclid`、`gclid`、域名大小写、默认端口等）。
- `published_at` 统一为 ISO 8601 + UTC。

### 两层去重

1. **canonical URL 去重**：SQLite 对 `canonical_url` 建 `UNIQUE INDEX`，重复插入自动拒绝。
2. **标题指纹**：NFKC 归一化 → 小写 → 去空白 → 去站点名后缀 → 去标点。记录在 `title_fp` 列，供未来使用。
3. 第一阶段**不做**跨网站“同一事件”语义去重（Reuters 和 BBC 报道同一事件应保留两篇）。

### 抓取礼仪

- 合理 User-Agent（含项目标识）
- timeout（默认 20s）
- 自动重试（默认 2 次，尊重 `Retry-After`）
- 域名请求间隔（`request_interval`，默认 1s）
- 不绕过登录 / 验证码 / 付费墙 / 访问控制
- 单篇失败不影响整批（错误写入 `status=error` + `errors` 字段，可下次重抓）

### 错误处理

一个网站 / 一篇文章失败不会终止整个任务：

```
HKEJ 成功 / Reuters 失败 / FT 成功
→ HKEJ 和 FT 正常入库，Reuters 记录错误，任务继续
```

至少记录：HTTP status、timeout、parsing error、extraction error、timestamp。

## 测试

```bash
uv run pytest -q
```

覆盖：RSS 解析、URL canonicalization、URL 去重、标题 fingerprint、Article 模型、SQLite 入库与唯一约束、Markdown/JSONL 导出、正文提取（含站点杂质清理）、HTML 栏目页发现与过滤。

## ECO 验收结果（2026-08-09）

| 指标 | 结果 |
| --- | --- |
| 新闻发现 | 26 篇（官方 RSS 22 篇 + Últimas 栏目页去重合并） |
| 成功下载 | 26 篇 |
| 成功提取正文 | 26 篇（status=ok） |
| 失败 | 0 篇 |
| 二次抓取去重 | 26 duplicates / 0 新增 |
| SQLite 最终存量 | 26 篇 |
| JSONL 导出 | `exports/articles.jsonl`（26 行） |
| Markdown 导出 | `exports/markdown/2026/08/*.md`（26 个） |

## 当前已知问题与下一步

- **ECO 官方 RSS 是主要来源**（WordPress `?feed`），但 RSS 中部分条目链接指向 `/entrevista/`、`/descodificador/` 等非标准日期 URL，已通过 URL 归一化与去重正确合并，无重复入库。
- 部分文章作者显示为 `ECO`（网站本身未标注个人作者），属正常站点信息。
- 下一步可依次扩展：Reuters / Financial Times / WSJ → AI 摘要 / 翻译 / 分类 → 公司实体识别 → 投资影响分析（代码结构已为 AI 层预留解耦接口）。
