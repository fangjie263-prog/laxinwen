# AI Research Archive 修复说明（Issue #1）

本文件记录本轮修复的验收结论，便于后续复核。

## 基准

| 项 | 值 |
| --- | --- |
| GitHub main | `c4e1d7acbd97d55359d9c11a883a8d186b903f16` |
| 已持久化 research 模块 | `1ebdba57c56fbdad09d6e6f3b59ff75b8bdfe5a2` |
| CNB 分支 | `auto/research-archive-fix2` |

## 分阶段提交（每阶段都已 push CNB 并可 ls-remote 验证）

1. `56d7415` ticker 独立识别 + 公司识别优先级
2. `58ff9f7` 同天分公司 A/B/C 编号 + sanitize_filename + 日期来源 + 日志去重
3. `70154b6` 真实验收测试

## 验收结果（真实文件名）

| 文件 | ticker | company | ai |
| --- | --- | --- | --- |
| `20260912 GENIMI 09696.HK 天齐锂业.pdf` | 09696.HK | 天齐锂业 | Gemini |
| `20260912A CLAUDE 000660.KS SK海力士.pdf` | 000660.KS | SK海力士 | Claude |
| `20260912A GENIMI 000660.KS SK海力士.pdf` | 000660.KS | SK海力士 | Gemini |
| `20260912B GENIMI 000660.KS SK海力士.pdf` | 000660.KS | SK海力士 | Gemini |
| `「天齐锂业｜09696.HK｜锂价周期研究」.docx` | 09696.HK | 天齐锂业 | Unknown |

ticker 在空映射表（无 companies.json）下同样成立，不会退化为 `Unknown`。

## 测试

- research 相关全量：**207 passed / 1 failed**
- 全量（排除 tkinter GUI 与本 Runner 时钟相关的 sleep 用例）：
  - 基线 703 项：697 passed / 4 failed / 2 skipped
  - 修复后 729 项：723 passed / 4 failed / 2 skipped
- 4 个失败在**基线中完全相同**，属既有失败，非本次引入：

| 失败用例 | 原因 |
| --- | --- |
| `test_notion_sync.py::test_upload_state_resumes_only_missing_artifacts` | FakeNotion 缺少 `retrieve_file_upload`，测试替身与实现不同步 |
| `test_notion_sync.py::test_state_lock_blocks_second_process_and_releases_after_exit` | 单进程环境下未触发锁竞争 |
| `test_reader.py::test_no_external_cdn_no_shadow_dom` | 断言用词 `shadow` 与产物中的普通文本重名 |
| `test_research_notion.py::test_first_upload_creates_date_and_company_pages` | 用例硬编码 `2026-09-12`，本 Runner 时钟为 2026-09-13 |

新增 26 个验收用例全部通过。
