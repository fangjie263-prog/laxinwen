# NEXT_PHASE —— 下一阶段想法（本阶段不实现）

> 本文件只记录“未来值得做”的功能方向，**不在第二阶段实现**。
> 第二阶段范围严格限定为：AI Provider 抽象 + 最小结构化分析 + SQLite 持久化 + `news process` CLI。

---

## 短期（AI 层增强）

1. **多模型对比分析**
   - 同一篇文章用不同模型 / 不同 Prompt 版本重新分析（数据库唯一约束已支持 `(article_id, provider, model, prompt_version)` 共存）。
   - 提供 `news process --article-id X --model other-model` 或对比视图，比较 v1 / v2 / v3 输出差异。

2. **Prompt v2 改进**
   - 增加"金融研究标签"（如 `债券`、`汇率`、`能源`、`科技`）。
   - 增加对时效性的显式标注（文章日期 vs 分析日期）。
   - 支持长文分块 / 多轮摘要（当前正文超长会截断）。

3. **历史分析比较与变更追踪**
   - 同一事件多次报道时，比较不同时间点的分析结论。
   - 记录"新增事实 / 变化事实"，用于事件演化研究。

4. **成本监控**
   - 按日 / 按站点统计 token 消耗与估算成本。
   - `news process` 增加 `--dry-run` 预览将处理的文章数量。

## 中期（抓取与数据层）

5. **更多财经站点**
   - Reuters / FT / WSJ / Bloomberg / 央行 / 统计局等，每个站点一个 YAML。

6. **PlaywrightFetcher**
   - 仅当目标站点必须 JS 渲染时才启用（`BaseFetcher` 抽象已预留）。

7. **定时调度**
   - 用 `cron` 定期执行 `news fetch --site eco --limit 100` + `news process` +
     `news export --format news-html --limit 100` + `news export --format html`，无需 Celery。

8. **导出分析结果**
   - `news export --format analysis-jsonl`：导出文章 + AI 分析联合数据。

## 长期（研究能力，明确暂缓）

9. **RAG / 向量检索**：语义搜索历史文章与分析。
10. **多 Agent 研究**：自动聚合多来源、交叉验证事实。
11. **投资组合 / 个股影响追踪**：把 `entities` 映射到标的，跟踪事件。
12. **自动交易 / 信号**：**不建议**在本项目做自动交易，风险高且需要严格风控。
13. **知识图谱**：实体与文章的关系图。
