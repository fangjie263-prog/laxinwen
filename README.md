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
（未来）AI 摘要 / 翻译 / 分类 / 研究分析
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
| `news status [--source <id>]` | 显示数据库与抓取状态 |
| `news export --format jsonl\|markdown [--source <id>] [--output DIR]` | 导出 |

环境变量（可选）：

- `NEWS_SITES_DIR`：站点配置目录（默认 `./sites`）
- `NEWS_DB`：SQLite 数据库路径（默认 `./data/news.db`）
- `NEWS_EXPORTS`：导出目录（默认 `./exports`）

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
│   ├── cli.py              # 命令行入口
│   ├── config.py           # 站点配置加载
│   ├── model.py            # 统一 Article 数据模型
│   ├── normalize.py        # URL 规范化 + 标题指纹
│   ├── storage.py          # SQLite 存储层
│   ├── discover.py         # 新闻发现（RSS → RSSHub → 栏目页）
│   ├── fetch.py            # 下载层（httpx，Fetcher 抽象）
│   ├── extract.py          # 正文提取（Trafilatura）
│   ├── pipeline.py         # 抓取 pipeline（串联各阶段）
│   └── export.py           # JSONL / Markdown 导出
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
- 下一步建议：
  1. 增加更多财经站点（Reuters/FT/WSJ 等）——每个站点新增一个 YAML；
  2. 为必须 JS 的站点实现 `PlaywrightFetcher`；
  3. 接入 RSSHub 公共实例或自建实例；
  4. 实现 `news fetch --retry-failed` 的定时调度（`cron` 即可，无需 Celery）；
  5. 未来 AI 层：摘要 / 翻译 / 分类 / 实体识别 / 市场影响分析（通过 `Article` 模型对接）。

---

## ECO 端到端验收结果

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
