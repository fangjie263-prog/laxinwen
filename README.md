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
    ↓
HTML 研究结果展示层（第三阶段）
    ↓
data/export/html/（单篇研究阅读页 + index.html 索引）
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
# --limit 的含义：向网站寻找“最近 N 篇新闻”的发现窗口（RSS + 栏目页 + load-more 补齐），
# 数据库持续累积，已存在的文章自动去重（不重复插入）。
uv run news fetch --site eco --limit 100 --timeout 20 --retries 3 --interval 2
uv run news fetch --site eco --limit 50
uv run news fetch --site eco --limit 200

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

# ---------- News Archive HTML（最近 N 条新闻阅读目录） ----------
# 直接读取 articles 表，不要求 AI 分析成功；未分析 / 失败文章都会显示。
# 默认输出到 data/export/news-html/<site>/，index.html 为阅读入口。
uv run news export --format news-html --site eco --limit 50
uv run news export --format news-html --site eco --limit 100
uv run news export --format news-html --site eco --limit 200

# ---------- HTML 研究结果展示（AI 分析） ----------
uv run news export --format html                       # 导出全部成功 AI 分析 → data/export/html/
uv run news export --format html --site eco           # 只导出 ECO
uv run news export --format html --article-id 1       # 只导出指定文章
uv run news export --format html --output /tmp/out    # 指定导出目录

# ---------- 便携式 HTML（独立 HTML / HTML 新闻包） ----------
# 独立 HTML：单个 self-contained .html，CSS/JS 全内嵌，双击即可阅读，无需 laxinwen/Python/本地服务器。
uv run news export --format portable --site eco --limit 100
uv run news export --format portable --site eco --limit 100 --output /tmp/HKEJ-2026-08-10.html

# HTML 新闻包：可复制到其它电脑的独立阅读目录（index.html + articles/NNN.html）
uv run news export --format package --site eco --limit 100
#   默认输出到 data/export/portable/<site>-<date>/
#   ├── index.html
#   └── articles/001.html ...

# 便携阅读包：给他人使用，双击 Open-Reader.bat，经 http://127.0.0.1 打开（兼容浏览器扩展）
uv run news export --format reader --site eco --limit 100
#   默认输出到 data/export/portable/Laxinwen-<SITE>-<date>/
#   ├── index.html
#   ├── articles/001.html ...
#   ├── server.py          # 内嵌的迷你本地 HTTP 服务器（纯 Python 标准库，只监听 127.0.0.1）
#   └── Open-Reader.bat    # Windows 双击启动器（无需安装 laxinwen，自动开浏览器）

# 所有导出的 HTML 展示时间统一为北京时间（Asia/Shanghai），24 小时制。

# ---------- Windows 桌面 GUI（Laxinwen News Reader） ----------
# 轻量级 tkinter 桌面窗口（Python 标准库，零额外依赖）
uv run news gui

# ---------- 测试 ----------
uv run python -m pytest          # 全部离线测试
uv run python -m pytest -m network   # 需要外网的在线测试
```

---

## Windows 桌面 GUI（Laxinwen News Reader）

> 第四阶段新增：把 CLI 的完整能力包成普通用户可直接双击使用的 Windows 桌面窗口。
> 新闻来源可在 **ECO / HKEJ / 全部** 之间切换，后台统一复用现有 pipeline / processor / export。
> **GUI 只是用户界面层**：抓取 / 去重 / AI 分析 / HTML 导出全部调用现有
> pipeline / processor / export，**绝不重新实现**一套抓取逻辑，也不破坏去重。

### 启动方式

**方式一：命令行**

```bash
uv run news gui                     # 默认初始来源 ECO
uv run news gui --site hkej         # 初始来源 HKEJ
uv run news gui --site all          # 初始来源全部
```

**方式二：Windows 双击**

```
双击 NewsReader.bat
```

- 自动 `cd` 到项目目录、检查 uv、首次自动 `uv sync`，然后 `uv run news gui`；
- 出错时窗口保持打开并显示错误码，方便排查；
- **不包含任何 API Key**，不修改环境变量中的敏感信息（AI 配置请放在项目根 `.env`）。

需要查看完整命令行日志时，双击 `NewsReader-Console.bat`（保留控制台窗口）。

### 主界面

| 区域 | 说明 |
| --- | --- |
| 新闻来源 | 下拉框：**ECO / HKEJ / 全部**。选择后所有按钮按当前来源执行；选择“全部”= ECO + HKEJ 分别执行 |
| 抓取数量 | 数字输入框（默认 100，支持任意正整数如 20/50/100/200/500，自动拦截 0/-1/abc）+ 快捷按钮 `[50] [100] [200]` |
| 抓取最新新闻 | ECO→`news fetch --site eco --limit N`；HKEJ→`news fetch --site hkej --limit N`；全部→两者分别执行。**异步执行**不卡界面 |
| 📖 打开新闻库 | 按当前来源导出 News Archive（`news export --format news-html --site <id> --limit N`），通过**本地 HTTP 阅读模式**用默认浏览器打开 `http://127.0.0.1:<port>/news-html/<id>/index.html`（全部→分别打开 ECO 与 HKEJ） |
| 🤖 AI 分析 | 输入分析数量（默认 3），按当前来源复用现有 `news process --site <id>` 的 AI processing 逻辑。**若未配置 AI，自动提示进入“⚙ AI 设置”** |
| ⚙ AI 设置 | 打开独立设置窗口，配置 Provider / API Base URL / API Key / Model，提供「测试连接」「保存」；保存后立即生效，无需重启 |
| 📊 打开 AI 研究结果 | 按当前来源导出 `news export --format html --site <id>`，通过**本地 HTTP 阅读模式**打开 `http://127.0.0.1:<port>/html/<id>/index.html` |
| 导出数量 | 数字输入框（默认 100），用于下方“导出”按钮 |
| 导出方式 | 下拉框：**📦 便携阅读包 / 📄 独立 HTML / 📚 HTML 新闻包**（**默认 = 便携阅读包**） |
| 导出 | **单一导出按钮**：根据「导出方式」下拉调用对应的现有导出器（便携阅读包 / 独立 HTML / HTML 新闻包），三种能力全部保留，只是 GUI 层统一为下拉选择 + 一个按钮 |
| 日志区 | 实时显示抓取/AI/导出过程与结果（发现/重复/新增/失败），全部来源时分别显示 `[ECO]` 与 `[HKEJ]`；并明确显示 `新闻库已启动：http://127.0.0.1:<port>/...` |
| 状态区 | 数据库路径、ECO 新闻数、HKEJ 新闻数、AI 已分析/失败、当前来源、最后操作、最后抓取时间（从现有 storage/status 读取，不硬编码） |

### 与 CLI 的对应关系

| GUI 按钮 | 等价 CLI |
| --- | --- |
| 抓取最新新闻 | `uv run news fetch --site eco|hkej --limit N`；全部→分别执行 ECO 与 HKEJ |
| 📖 打开新闻库 | `uv run news export --format news-html --site eco|hkej --limit N` → 浏览器打开 `http://127.0.0.1:<port>/news-html/<id>/index.html` |
| 🤖 AI 分析 | `uv run news process --site eco|hkej --limit N`（复用现有 AI provider） |
| 📊 打开 AI 研究结果 | `uv run news export --format html --site eco|hkej` → 浏览器打开 `http://127.0.0.1:<port>/html/<id>/index.html` |
| 导出（📦 便携阅读包） | `uv run news export --format reader --site eco|hkej --limit N`（index.html + articles + server.py + Open-Reader.bat） |
| 导出（📄 独立 HTML） | `uv run news export --format portable --site eco|hkej --limit N`（单个自包含 HTML，双击可读） |
| 导出（📚 HTML 新闻包） | `uv run news export --format package --site eco|hkej --limit N`（index.html + articles/NNN.html） |

> **本地 HTTP 阅读模式**：GUI 启动一个轻量级 localhost HTTP 静态服务器
> （Python 标准库 `http.server`，**只监听 127.0.0.1**，端口被占用自动选择可用端口），
> 把 `data/export/` 作为静态目录提供。GUI 关闭时服务器自动停止。
> 这样浏览器扩展（如 Immersive Translate）能像处理普通网页一样处理本地阅读页面
> （`file://` 下扩展无法可靠工作）。
>
> 去重保证：GUI 的“抓取最新新闻”调用现有 `discover → deduplicate → fetch → extract → storage`
> 完整 pipeline。数据库已有 100 篇时再次抓取最近 100 篇，结果为 `发现 100 / 重复 100 / 新增 0`，
> 绝不重新下载已入库文章。

### GUI 测试

```bash
# 无头环境（Linux CI）需要虚拟显示：
xvfb-run -a uv run python -m pytest tests/test_gui.py -v
# 全部测试（含 GUI）
xvfb-run -a uv run python -m pytest
```

GUI 测试覆盖：默认抓取数量 100、快捷按钮 50/100/200、非法数量拦截、调用正确 pipeline、
新闻库/研究按钮打开 `http://127.0.0.1`（不是 `file://`）、AI 分析复用现有 processor、
pipeline 出错不崩溃且按钮恢复、status 读取数据库统计、GUI 关闭时 HTTP 服务器自动停止。

本阶段新增的 GUI 测试（`tests/test_gui.py::TestPortableExportButtons` / `TestAiSettings`
与 `tests/test_ai_config_store.py`）覆盖：

- 导出：默认导出方式 = 📦 便携阅读包；下拉选择 portable → 调用 portable reader、
  independent HTML → 调用独立 HTML、package → 调用 HTML 新闻包；非法数量拦截；导出失败不崩溃；
- AI 配置：未配置时点「AI 分析」→ 提示进入「⚙ AI 设置」；填写配置 → 保存 → 配置可被 provider 读取；
  修改配置 → 保存 → 新配置立即生效；API Key 只显示掩码、日志不出现 API Key；
- 测试连接：401 → 显示 API Key 无效；404/model_not_found → 显示 Model 错误；网络错误 → 显示连接错误；
  （`tests/test_ai_config_store.py`，全部离线 mock，不访问真实网络）

---

## 三种 HTML 导出方式

Laxinwen 提供三种 HTML 导出方式，底层三个导出器全部保留，GUI 通过「导出方式」下拉选择 + 单一「导出」按钮调用。

### 1. 📦 便携阅读包 —— 推荐

**推荐日常使用。**

输出目录：`data/export/portable/Laxinwen-<SITE>-<date>/`

```
Laxinwen-ECO-2026-08-10/
├── index.html
├── articles/
├── server.py
└── Open-Reader.bat
```

- 最适合日常使用；
- 可以**整个文件夹复制到另一台电脑**；
- 不需要安装 Laxinwen；
- 通过 `127.0.0.1` 本地 HTTP 打开（`Open-Reader.bat` 自动起服务器）；
- 浏览器把它视为正常网页，**最适合沉浸式翻译等浏览器扩展**；
- Windows 用户可以双击 `Open-Reader.bat`；
- 目标电脑需要 Python 3；
- 不包含 API Key；
- 不依赖 Laxinwen SQLite 数据库。

### 2. 📄 独立 HTML

输出文件：`data/export/portable/<site>-<date>.html`（如 `eco-2026-08-10.html`）

- 只有一个文件；
- CSS / JS / 新闻内容全部内嵌；
- 不需要 Laxinwen；
- 最方便发送、存档；
- 可以直接双击打开；
- 但是使用 `file://` 打开时，部分浏览器扩展可能无法正常工作，
  **因此不作为沉浸式翻译的首选方案**。

> 适合快速分享和单文件存档。

### 3. 📚 HTML 新闻包

输出目录：`data/export/portable/<site>-<date>/`

```
eco-2026-08-10/
├── index.html
└── articles/
    ├── 001.html
    ├── 002.html
    └── ...
```

- 新闻按照多个 HTML 文件保存；
- 适合长期归档；
- 方便后续单独处理文章；
- 不需要 Laxinwen；
- 直接打开 `index.html` 通常是 `file://`，
  **因此沉浸式翻译兼容性不如便携阅读包**。

> 适合完整新闻档案保存。

---

## AI 配置（GUI 内置设置中心）

普通用户**无需打开 PowerShell**，直接在 GUI 内完成 AI 配置：

1. 点击主界面「**⚙ AI 设置**」；
2. 填写 **Provider / API Base URL / API Key / Model**（Provider 不限于下拉，任意 OpenAI-compatible 均可手输）；
3. 点击「**测试连接**」真正发送一次极短模型请求，验证 Base URL + API Key + Model 是否可用；
4. 测试成功后点击「**保存**」，配置立即生效，无需重启；
5. 直接点击「**🤖 AI 分析**」即可使用。

若尚未配置 AI 就点击「AI 分析」，程序会提示进入「AI 设置」（而不是只显示“缺少 AI_MODEL”）。

**API Key 安全**：

- API Key 仅用于本地 AI 请求，**不写入新闻数据库、HTML 导出文件或日志**；
- 配置保存到项目根 `.env`（`gitignore` 已排除 `.env`，**不会进入 Git**）；
- 保存时逐字段更新 / 追加，**保留其它未知配置、不删除 CNB_TOKEN**；
- GUI 状态只显示掩码（如 `sk-****abcd`）。

---

## CLI 命令一览

| 命令 | 说明 |
| --- | --- |
| `news gui [--site <id>] [--db PATH]` | 启动 Windows 桌面 GUI（Laxinwen News Reader，来源 ECO/HKEJ/全部） |
| `news serve [--export-root DIR]` | 启动本地 HTTP 阅读服务器（仅 127.0.0.1，端口自动选择，Ctrl+C 停止） |
| `news fetch [--site <id>] [--limit N] [--timeout S] [--retries N] [--interval S] [--retry-failed]` | 抓取新闻（`--limit` = 最近 N 篇发现窗口） |
| `news list [--source <id>] [--limit N]` | 列出最近新闻 |
| `news status [--source <id>]` | 显示数据库与抓取状态（含 AI 分析统计） |
| `news process [--site <id>] [--limit N] [--article-id <id>] [--retry-failed]` | AI 处理已入库文章（生成结构化分析） |
| `news export --format jsonl\|markdown [--source <id>] [--output DIR]` | 导出（JSONL / Markdown） |
| `news export --format news-html [--site <id>] [--limit N]` | 导出 News Archive HTML（最近 N 条阅读目录） |
| `news export --format html [--site <id>] [--article-id <id>] [--output DIR]` | 导出 AI 研究结果 HTML |
| `news export --format portable [--site <id>] [--limit N] [--output FILE]` | 导出**独立 HTML**（单个 self-contained，双击可读，无需 laxinwen/Python/服务器） |
| `news export --format package [--site <id>] [--limit N] [--output DIR]` | 导出 **HTML 新闻包**（index.html + articles/NNN.html，可复制到其它电脑） |
| `news export --format reader [--site <id>] [--limit N] [--output DIR]` | 导出 **便携阅读包**（index.html + articles + server.py + Open-Reader.bat，给他人双击 Open-Reader.bat 经 localhost 打开） |

> **北京时间展示**：所有导出的 HTML（News Archive / AI Research / 独立 HTML / 新闻包）中的发布时间
> 统一使用 **Asia/Shanghai（北京时间），24 小时制**。数据在 SQLite 内仍统一存 UTC（ISO 8601），
> 仅在展示层转换为北京时间。

环境变量（可选）：

- `NEWS_SITES_DIR`：站点配置目录（默认 `./sites`）
- `NEWS_DB`：SQLite 数据库路径（默认 `./data/news.db`）
- `NEWS_EXPORTS`：导出目录（默认 `./exports`；HTML 默认输出 `./data/export/html/`）
- AI 相关变量见下方 [AI Processing Layer](#ai-processing-layer) 一节

---

## 项目结构

```
laxinwen/
├── pyproject.toml          # uv 项目定义、依赖、CLI 入口
├── README.md
├── .gitignore
├── NewsReader.bat             # Windows 双击启动器（GUI）
├── NewsReader-Console.bat     # Windows 控制台启动器（查看完整命令行日志）
├── sites/                  # 站点配置（一个网站一个 YAML）
│   ├── eco.yaml            # ECO – Economia Online（已跑通）
│   └── hkej.yaml           # HKEJ 信报（预留，见“已知问题”）
├── src/news/
│   ├── __init__.py
│   ├── cli.py              # 命令行入口（fetch / list / status / gui / process / export）
│   ├── config.py           # 站点配置加载
│   ├── model.py            # 统一 Article 数据模型
│   ├── normalize.py        # URL 规范化 + 标题指纹
│   ├── storage.py          # SQLite 存储层（articles + article_analysis）
│   ├── discover.py         # 新闻发现（RSS → RSSHub → 栏目页）
│   ├── fetch.py            # 下载层（httpx，Fetcher 抽象）
│   ├── extract.py          # 正文提取（Trafilatura）
│   ├── pipeline.py         # 抓取 pipeline（串联各阶段）
│   ├── export.py           # JSONL / Markdown 导出
│   ├── html_export.py      # HTML 研究结果展示层（第三阶段）
│   ├── news_archive.py     # News Archive Daily Reader（第三+五阶段）
│   ├── portable.py         # 便携式 HTML 导出（独立 HTML / HTML 新闻包 / 便携阅读包，第七阶段）
│   ├── beijing.py          # 北京时间（Asia/Shanghai）展示辅助（第七阶段）
│   ├── reader_server.py    # 本地 HTTP 阅读模式（第五阶段，仅 127.0.0.1）
│   ├── gui.py              # Windows 桌面 GUI（Laxinwen News Reader，第四+六阶段，tkinter）
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

1. **官方 RSS / Atom**（`rss:`）—— 第一优先（最快、含正文）；
2. **RSSHub**（`rsshub:`）—— 没有官方 RSS 时检查已有 Route；
3. **公开栏目页**（`lists:`）—— 前两者都不适用时才用 selectolax 解析；
4. **“加载更多”分页接口**（`load_more:`）—— RSS/栏目页不足时，通过站点 AJAX load-more 批量补齐最近 N 篇；
5. 站内搜索 —— 第一阶段不实现。

> **合并而非中断**：多个发现来源会**合并去重**，直到候选文章达到 `--limit`（最近 N 篇的发现窗口），
> 而不是“遇到一个来源就停止”。`canonical_url` 去重保证 RSS 与栏目页/load-more 重叠时不会重复。
> 某个来源失败（如 load-more nonce 过期 / 接口变更）不会中断其它来源。

> 增加一个“有官方 RSS 的简单网站”：在 `sites/` 下新增一个 YAML 即可，
> **无需修改核心 Python 代码**。

### ECO load-more（“Carregar mais artigos”）

ECO 的 `/ultimas/` 页面底部有“Carregar mais artigos”按钮，真实机制（已通过网页调查确认）：

- 点击后向 `https://eco.sapo.pt/wp-admin/admin-ajax.php` 发送 **GET** 请求；
- 参数：`action=eco_ajax_get_posts_latest`、`eco_offset=<n>`、`nonce=<nonce>`；
- `nonce` 静态写在 `/ultimas/` 首页 HTML 内嵌的 `ECO_JS` 变量中（无需 Cookie / 无需 JS 渲染，httpx 可直接调用）；
- 每次返回 **12 篇**新文章（JSON `data.posts_html` 卡片），offset 从首页文章数开始逐页 +12；
- ECO 归档很深（offset 上万仍有文章），`--limit` 决定取多少篇。

对应 `sites/eco.yaml` 配置：

```yaml
load_more:
  endpoint_selector: "button.js-archive-load-more"
  js_var: "ECO_JS"
  offset_param: "eco_offset"
  action_param: "action"
  nonce_param: "nonce"
  nonce_key: "nonce_load_more"
  url_key: "wp_ajax_url"
  per_page_key: "archive_load_more"
```

> 其他网站若有类似 AJAX load-more，只需按此结构配置（选择器 / 参数名 / JS 变量 key 可配置），
> 核心代码不硬编码任何 ECO 专属规则。

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

## HTML 研究结果展示层

第三阶段新增能力：把 SQLite 中**已经成功完成 AI 分析**的记录渲染成适合金融研究员阅读的 HTML。

```
SQLite (article_analysis, status='ok'/'success')
   ↓
HTML 研究阅读页面
   ↓
data/export/html/
   ├── index.html                    # 总索引（按日期倒序 + 成功/失败统计）
   └── YYYY/MM/0001-<slug>.html     # 每篇文章一个页面
```

### 用法

```bash
uv run news export --format html                # 导出全部成功分析
uv run news export --format html --site eco    # 只导出 ECO
uv run news export --format html --article-id 1  # 只导出指定文章
uv run news export --format html --output /tmp/out
```

默认输出到 `data/export/html/`，按 `YYYY/MM/` 组织。单篇文件名使用安全的
slug/canonical article id（如 `0001-eclipse-solar-tudo-o-que-precisa-de-saber.html`），
不会使用未经清理的原标题，也不包含 Windows 非法字符。

### 页面内容

每篇 HTML 包含（自上而下）：

1. **来源徽标 + 标题**；
2. **原文信息**：作者、发布日期、来源、原文链接；
3. **AI 中文摘要**（`summary_zh`，完整显示不截断）；
4. **关键观点**（编号列表，`key_points_json`）；
5. **主题**（chip 标签，`topics_json`）；
6. **实体**（表格，`entities_json`，`location` 等合法类型正常显示）；
7. **市场相关性**（HIGH / MEDIUM / LOW + 理由，明确标注“AI 判断，不代表原文事实”）；
8. **原文语言**；
9. **AI Processing Metadata**（provider / model / prompt_version / token usage / cost / created / updated）；
10. **原文**（“查看原文 →”链接 + 原文正文，与 AI 分析明显分区）。

### 设计约束

- 纯 Python + HTML + CSS，HTML5 / UTF-8；
- 中文与葡萄牙语重音字符正常显示；
- **不依赖外部 CDN / 字体 / JS**，可直接双击打开阅读；
- 页面宽度适合桌面阅读，正文阅读宽度不设过宽；
- **只导出 `status='ok'/'success'` 的成功分析**；失败记录不出现在正常研究页面，
  仅由 `index.html` 显示“成功 N / 失败 N”统计；
- 用户/文章内容全部 **HTML escape**，正文中的 HTML 不会破坏页面结构；
- 不修改 `article_analysis` schema，不引入 React/Vue/前端构建/Web server/ORM/RAG。

### 测试

```bash
uv run python -m pytest tests/test_html_export.py -v
```

覆盖：单篇生成、中文摘要、葡语重音、key_points / topics / entities（含 location）、
市场相关性、provider/model/prompt version、token usage、cost、canonical_url 链接、
失败文章不进入正常 HTML、index.html、文件名安全、空字段容错、HTML escape 等。

---

## News Archive HTML（最近 N 条新闻阅读目录）

```bash
uv run news export --format news-html --site eco --limit 100
uv run news export --format news-html --site eco --limit 50
uv run news export --format news-html --site eco --limit 200
```

默认输出到 `data/export/news-html/<site>/`，`index.html` 是阅读入口。

> **第五阶段升级：Daily Reader 阅读器**。`index.html` 采用与项目 daily HTML
> 一致的标准阅读器设计语言：窄版居中（约 720px 白色阅读区）、衬线正文、
> 顶部 `ECO News — Daily Reader` 标题区、Table of Contents、每篇一个
> `<section id="article-N">` 连续阅读、已读/收藏/阅读进度/J·K 快捷键/阅读模式切换，
> 阅读状态保存在 `localStorage`。

### 与 AI Research HTML 的区别

| | News Archive（news-html） | AI Research（html） |
| --- | --- | --- |
| 数据源 | `articles` 表（全部新闻） | `article_analysis` 成功记录 |
| 要求 AI 成功 | ❌ 不要求 | ✅ 只显示成功分析 |
| 未分析文章 | 显示（○ 尚未分析） | 不显示 |
| 失败文章 | 显示（⚠ 失败） | 不显示（仅 index 统计） |
| 用途 | 最近 N 条新闻阅读目录 | 研究结果报告 |

### 页面内容

`index.html`（按 `published_at DESC`，**Daily Reader 风格**）：

- 顶部标题区：`ECO News — Daily Reader` / 日期 / `N articles · ECO – Economia Online`；
- **Table of Contents**：N 篇新闻全部列出，点击标题跳转到对应 `<section id="article-N">`；
- 每篇 section 连续阅读：标题 / 发布时间 / 来源 / 作者 / AI 中文摘要（如已分析）/ 原文正文 / 原文链接 / `↑ Back to Contents`；
- **AI 状态三态**：`✓ AI 已分析` / `⚠ AI 分析失败` / `○ 尚未分析`（绝不把失败伪装成成功）；
- **阅读器交互**（`localStorage` 保存）：已读 `□/✓`、收藏 `☆/★`、阅读进度条、
  `J/K` 上下篇快捷键、Day/Sepia/Night 阅读模式切换。

单篇页（`YYYY/MM/0001-<slug>.html`，同样 Daily Reader 风格）：

- 有 AI：AI 中文摘要 / 关键观点 / 主题 / 实体 / 市场相关性 / 原文语言 + 原文正文；
- 无 AI：显示“尚未进行 AI 分析” + 原文正文；
- AI 失败：显示“AI 分析失败”提示 + 原文正文。

### 设计约束

- 纯 Python + HTML + CSS + 少量内嵌 JS，HTML5 / UTF-8，无外部 CDN / 字体；
- **浏览器扩展兼容**：正文是标准 HTML DOM 文本（`article/section/p/h1/h2`），
  不塞进 canvas/iframe/图片、不用 shadow DOM、不依赖外部 CDN、不用复杂 JS 框架；
  原文葡语可被浏览器扩展（如 Immersive Translate）正常识别和翻译，
  AI 中文摘要保持中文不重复翻译；
- 全部内容 HTML escape；
- `--limit` 从 SQLite 按 `published_at DESC` 取最近 N 篇（数据库可以有 5000 篇，
  `--limit 100` 只显示最近 100 篇）；对 50/100/200 篇都保持可读，不截断成摘要卡片；
- 不修改 `article_analysis` schema，不引入复杂前端。

### 本地 HTTP 阅读模式

GUI“打开新闻库 / 打开 AI 研究结果”使用 Python 标准库 `http.server` 启动
**只监听 `127.0.0.1`** 的轻量级静态服务器，把 `data/export/` 作为静态目录：

```
http://127.0.0.1:<port>/news-html/eco/index.html   # 新闻库
http://127.0.0.1:<port>/html/index.html            # AI 研究结果
```

- 端口被占用时自动选择可用端口；GUI 关闭时服务器自动停止；
- 禁止监听 `0.0.0.0`，不会把本地新闻数据库暴露到局域网；
- 不实现翻译引擎、不把 Immersive Translate 硬编码进 HTML。

### 测试

```bash
uv run python -m pytest tests/test_news_archive.py tests/test_reader.py -v
```

覆盖：daily 风格 HTML（窄版居中/衬线/标题区/目录/anchor）、100 篇目录与
`article-N` 锚点、已读/收藏/localStorage、中文摘要/葡语正文/HTML escape、
localhost server 可启动、只监听 `127.0.0.1`、端口占用自动换端口、
GUI 打开 `http://127.0.0.1`（不是 `file://`）、原有 `--format html` 无回归等。

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
- **HTML 导出已知限制**：只导出 `status='ok'/'success'` 的成功分析；失败文章仅
  在 `index.html` 显示统计，不出现在正常研究页面（符合设计）。
- **News Archive（news-html）**：直接读取 `articles` 表，未分析 / 失败文章都会显示；
  AI 状态三态（✓ / ⚠ / ○）绝不混淆。
- 下一步建议（详细见 `NEXT_PHASE.md`）：
  1. 增加更多财经站点（Reuters/FT/WSJ 等）——每个站点新增一个 YAML；
  2. 为必须 JS 的站点实现 `PlaywrightFetcher`；
  3. 接入 RSSHub 公共实例或自建实例；
  4. 用 `cron` 定时调度 `news fetch` + `news process` + `news export --format news-html --limit 100` +
     `news export --format html`；
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

---

## 第三阶段：ECO 发现升级 + News Archive 验收结果

真实运行验证（本次 PR）：

| 项目 | 结果 |
| --- | --- |
| 离线测试 | **164 项全部通过**（原 132 + 新增 32） |
| `news fetch --site eco --limit 100` | 发现 **100 篇**（RSS 22 + 首页 26 + load-more 补齐） |
| 下载 / 提取 | **100 / 100**，失败 0 |
| 二次 `--limit 100` | **100 篇全部 skipped_dup**（0 新插入，数据库保持 100） |
| `news export --format news-html --limit 50` | 显示最近 **50** 条 |
| `news export --format news-html --limit 100` | 显示最近 **100** 条 |
| News Archive AI 状态 | 3 已分析 / 1 失败 / 96 未分析（三态正确显示） |
| AI Research 链接 | index 3 个 + 单篇页均指向 `data/export/html/` 对应研究页（文件存在） |
| `news export --format html` | 继续正常（只显示 3 篇成功分析） |
| 是否使用 Playwright | ❌ 不需要（httpx + selectolax 即可，已真实验证） |

> 说明：本次真实验收中用 `news process --site eco --limit 3` 通过 CNB AI 网关真实处理了
> 3 篇 ECO 文章（成功 3），并人工构造 1 篇 failed 分析记录，用于验证 News Archive 的
> AI 状态三态显示。数据库的 AI 分析记录可随时用 `news process` 补充或删除。

---

## 第六阶段：ECO + HKEJ 整合进 Windows GUI 验收结果

真实运行验证（本次 PR，Xvfb 虚拟显示下运行真实 tkinter 窗口）：

| 项目 | 结果 |
| --- | --- |
| 离线测试 | **248 项全部通过**（新增多来源 GUI 测试） |
| `uv run news gui` | 正常启动 GUI（标题 “Laxinwen News Reader”） |
| 新闻来源下拉框 | **ECO / HKEJ / 全部** 三选一（默认 ECO，可切换） |
| 抓取数量 | 默认 100，支持 50/100/200 快捷按钮，可输入任意正整数（20/500 等，拦截 0/-1/abc） |
| ECO 抓取（limit=50） | 调用 `pipeline.run_site("eco")`（等价 `news fetch --site eco --limit 50`） |
| HKEJ 抓取（limit=50） | 调用 `pipeline.run_site("hkej")`（复用现有 HKEJ adapter，等价 `news fetch --site hkej --limit 50`） |
| 全部抓取（limit=50） | ECO 与 HKEJ 分别执行 limit=50，日志分别显示 `[ECO] 发现` / `[HKEJ] 发现` |
| 📖 打开新闻库 | ECO→`/news-html/eco/index.html`；HKEJ→`/news-html/hkej/index.html`；全部→两者分别导出并打开（非 file://，本地 HTTP 阅读模式） |
| 🤖 AI 分析（数量 3） | ECO→`process_batch(source_id="eco")`；HKEJ→`process_batch(source_id="hkej")`；全部→两者分别执行（复用现有 AI processing） |
| 📊 打开 AI 研究结果 | ECO→`html/eco/index.html`；HKEJ→`html/hkej/index.html`（`news export --format html --site <id>`，本地 HTTP 阅读模式） |
| 状态区 | 数据库、ECO 新闻、HKEJ 新闻、AI 已分析、AI 失败、当前来源、最后操作、最后抓取时间（从 SQLite 真实读取，不硬编码） |
| 错误处理 | ECO HTTP 500 / HKEJ HTTP 500 / AI HTTP 401 均在日志显示“XX 失败”，GUI 不崩溃、按钮恢复可再点 |
| 是否修改 HKEJ adapter | ❌ 本 PR 未改 `sources/hkej.py`、`sites/hkej.yaml` |
| 是否修改 Daily Reader | ❌ 本 PR 未改 `news_archive.py`、`reader_server.py` |
| 是否修改 AI / schema | ❌ 未改 `processor.py` / `provider.py` / `schema.py` / 数据库 schema |
| 是否引入新依赖 | ❌ 仅 Python 标准库 `tkinter/ttk/threading/queue` |

---

## 第五阶段：News Archive Daily Reader + 本地 HTTP 阅读模式 验收结果

| 项目 | 结果 |
| --- | --- |
| 离线测试 | **197 项全部通过**（原 179 + 新增 18） |
| News Archive 新版 daily 风格 | ✅ 窄版居中 720px 白色阅读区、浅灰背景、衬线正文、宽行距 |
| 顶部标题区 | ✅ `ECO News — Daily Reader` / 日期 / `N articles · ECO – Economia Online` |
| Table of Contents | ✅ 100 篇全部列出，点击标题跳转到 `#article-N` |
| 连续阅读 section | ✅ `<section id="article-N">`，非卡片列表；含标题/时间/来源/作者/AI摘要/原文正文/原文链接/Back to Contents |
| AI 三态 | ✅ ✓ 已分析 / ⚠ 失败 / ○ 未分析，失败/未分析不影响原文阅读 |
| 阅读器交互 | ✅ 已读 □/✓、收藏 ☆/★、阅读进度条、localStorage、Back to Contents、J/K 快捷键、Day/Sepia/Night 阅读模式 |
| 50/100/200 篇可读性 | ✅ 不截断成摘要卡片 |
| 本地 HTTP 阅读模式 | ✅ GUI 打开 `http://127.0.0.1:<port>/news-html/eco/index.html` 与 `http://127.0.0.1:<port>/html/index.html`（非 file://） |
| 只监听 127.0.0.1 | ✅ 禁止 0.0.0.0（有测试覆盖） |
| 端口被占用自动换端口 | ✅ 有测试覆盖 |
| GUI 关闭自动停止服务器 | ✅ 有测试覆盖 |
| 浏览器扩展兼容 | ✅ 标准 UTF-8、语义化 article/section/p/h1/h2、无 canvas/iframe/shadow DOM、无外部 CDN、无复杂 JS 框架 |
| 不实现翻译引擎 | ✅ 不把 Immersive Translate 硬编码进 HTML |

> 说明：daily HTML 参考文件 `daily(20260809-204743).html` 未在仓库/Issue 附件中获取到，
> 因此严格按其列出的"标准阅读器设计语言"实现：窄版居中、衬线正文、标题区、Table of Contents、
> `article-N` section、已读/收藏/进度/localStorage/J·K/阅读模式、Back to Contents。
