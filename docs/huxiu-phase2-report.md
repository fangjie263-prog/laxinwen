# Huxiu Prototype — Phase 2 stability report

验证日期：2026-09-12。分支：`codex/huxiu-prototype`。本阶段没有修改
main、Article schema、数据库、GUI、Scheduler 或其他新闻源。

## 【已验证】Discovery 与分页

真实访问了：

- `/article/`
- `/article/?page=2`
- `/article/?page=3`
- `/article/page/2`
- `/article/page/3`

结果：

- `/article/`：HTTP 200，12 个文章 ID
- `?page=2`：HTTP 200，但与第一页返回相同 12 个 ID
- `?page=3`：HTTP 200，但与第一页返回相同 12 个 ID
- `/page/2`：HTTP 404
- `/page/3`：HTTP 404

结论：没有发现稳定的历史分页方法。当前不能把 query/path 形式注册为
生产分页接口；`stable=false` 已记录在
[pagination-analysis.json](../huxiu-recon/pagination-analysis.json)。

这是本阶段最重要的发现：当前 `/article/` 适合“最新文章窗口”，不适合
未经进一步研究的历史回溯抓取。

## 【已验证】发布时间

20 篇真实文章均有可解析时间：

- 可见 `.article__time`：`YYYY-MM-DD HH:mm`
- `article:published_time`：带 `+08:00` 的 ISO 时间
- 统一输出为 UTC aware `datetime`
- 没有把抓取时间当发布时间

示例：`2026-09-13T00:09:43+08:00` →
`2026-09-12T16:09:43+00:00`。

完整原始值和解析值见
[time-analysis.json](../huxiu-recon/time-analysis.json)。

## 【已验证】URL canonicalization

20 篇文章验证了：

- desktop URL
- mobile URL
- `utm` query
- fragment
- trailing slash
- `og:url`

所有变体均归一到：

```text
https://www.huxiu.com/article/<id>.html
```

`all_normalized_stable=true`，详情见
[url-analysis.json](../huxiu-recon/url-analysis.json)。

## 【部分验证】转载来源

20 篇中 10 篇出现 `.article__reprinted-explain` 网络来源提示。解析器可以
稳定识别“来源于网络”，但这批样本没有稳定给出具体外部媒体名称；因此没有
猜测或伪造 `original_source`，只保存：

```text
网络来源（页面未给出媒体名）
```

没有验证出 5 个可命名的外部 publisher。详情见
[original-source-analysis.json](../huxiu-recon/original-source-analysis.json)。

## 【已验证】图片

20 篇真实文章中：

- 15 篇含正文图片
- 7 篇含图片说明
- 至少 1 篇含 24 张正文图片
- 10 个真实图片 URL 做了串行 HTTP GET
- 10/10 HTTP 200
- 10/10 `image/png`

图片支持 `src`、`data-src`、`_src`，并统一为绝对 URL；图片和 caption 保持
正文顺序。详情见
[image-analysis.json](../huxiu-recon/image-analysis.json)。

## 【已验证】低频网络稳定性

主稳定性运行使用正常 User-Agent、串行请求、文章间隔 1 秒、图片间隔 0.5
秒，没有代理、并发、验证码绕过或 challenge 绕过：

```text
channel/home/article requests: 23
HTTP 200: 23
HTTP 403: 0
HTTP 429: 0
timeout: 0
```

20 篇文章全部 HTTP 200、全部成功提取。完整日志见
[network-stability.json](../huxiu-recon/network-stability.json)。这证明了本次
低频窗口的稳定性，但不等价于长期无限运行保证。

## 【已验证】20 篇真实 fixture 覆盖

已从真实网络保存并复制 20 篇文章 HTML 到测试 fixture，不使用 mock HTML。
覆盖统计：

| 结构 | 真实样本数 |
|---|---:|
| 有正文图片 | 15 |
| 有图片说明 | 7 |
| 有 heading | 16 |
| 有 blockquote | 4 |
| 有 list | 0 |
| 最短正文 | 2155 字符 |

样本没有观察到 list，因此没有人为制造 list fixture。20 篇详细统计见
[phase2-samples.json](../huxiu-recon/phase2-samples.json)。

## 【已验证】Parser robustness

新增测试覆盖：

- title / author / published_at / canonical_url
- section / body_text / body_html
- images / lead image / caption
- heading / blockquote / block order
- 广告、footer、相关推荐、热门文章、导航污染排除
- original source 的保守识别
- empty body protection
- 20 篇以上真实 HTML fixture

结果：

```text
python -m pytest tests/test_huxiu.py -q
13 passed
```

## 【已验证】Trafilatura fallback

处理顺序现在是：

```text
Huxiu 专用 Parser
    ↓ 仅当标题缺失或正文异常
现有通用 Trafilatura Extractor
    ↓ 仍然复用 Laxinwen 现有 generic path
失败则返回 False
```

健康的 Huxiu 结果不会被 Trafilatura 覆盖，因此图片 block 顺序不会丢失。
20 篇真实页面均由 Huxiu 专用 Parser 成功处理；fallback 通过空正文保护测试，
但尚未遇到真实 Huxiu 页面触发 fallback 的案例。

## 【发现的问题】

1. 当前文章频道的 `?page=2/3` 不是分页，而是重复第一页。
2. 当前样本只能稳定识别“网络来源”，无法稳定还原外部媒体名称。
3. 20 篇样本没有 list 结构，因此 list 解析尚未被真实样本验证。
4. 当前低频运行不能证明长期运行不会遇到 403/429。

## 【建议正式整合】

结论：**暂不建议直接进入长期正式整合**。

建议保留当前 Prototype，下一阶段先补充：

- 通过真实 Nuxt API/游标机制找到可靠历史发现方式；
- 找到至少 5 篇能明确标出外部媒体名称的转载文章；
- 观察更长时间窗口的限流行为；
- 获取真实 list 结构样本，或明确将 list 标为未支持结构。

当前可以作为“最新文章窗口”的候选 Adapter，但还不应承诺完整历史分页和
稳定转载来源字段。
