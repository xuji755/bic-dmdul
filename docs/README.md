# bic-dmdul Documentation / bic-dmdul 文档中心

This page is the documentation hub for `bic-dmdul`. It keeps the original research notes intact while providing bilingual navigation for every technical document in this repository.

本页是 `bic-dmdul` 的文档中心。现有探索笔记保留原文件名和原始证据上下文，同时通过双语索引覆盖仓库内全部技术文档，便于中英文阅读。

Website / 官方网站: [www.dbaiops.com](https://www.dbaiops.com)

## Language Entry / 语言入口

- [中文文档索引](README_CN.md)
- [English Documentation Index](README_EN.md)

## Recommended Reading Order / 推荐阅读顺序

| Order | 中文 | English |
| --- | --- | --- |
| 1 | [中文使用手册](USER_MANUAL_CN.md) | [Operator guide](README_EN.md#operator-guide) |
| 2 | [项目目标](PROJECT_GOAL.md) | [Project goal](PROJECT_GOAL.md) |
| 3 | [DM8 存储格式阶段性总结](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md) | [Storage format summary](README_EN.md#storage-architecture-and-format) |
| 4 | [Bootstrap 与标准表下载设计](DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md) | [Bootstrap and table download design](README_EN.md#bootstrap-dictionary-and-offline-download) |
| 5 | [存储架构笔记](DM8_STORAGE_ARCHITECTURE_NOTES.md) | [Storage architecture notes](DM8_STORAGE_ARCHITECTURE_NOTES.md) |
| 6 | [页结构笔记](DM8_PAGE_STRUCTURE_NOTES.md) | [Page structure notes](DM8_PAGE_STRUCTURE_NOTES.md) |
| 7 | [类型存储笔记](DM8_TYPE_STORAGE_NOTES.md) | [Type storage notes](DM8_TYPE_STORAGE_NOTES.md) |
| 8 | [证据采集流程](EVIDENCE_CAPTURE_WORKFLOW.md) | [Evidence capture workflow](EVIDENCE_CAPTURE_WORKFLOW.md) |
| 9 | [探索任务清单](EXPLORATION_TASKS.md) | [Exploration and implementation tasks](EXPLORATION_TASKS.md) |

## Complete Document Map / 全量文档地图

| Document | Type | Primary Language | 中文阅读说明 | English Reading Guide |
| --- | --- | --- | --- | --- |
| [USER_MANUAL_CN.md](USER_MANUAL_CN.md) | Manual | Chinese | 命令行使用手册，覆盖 bootstrap、导出、导入、LOB、分区、TRUNCATE/DROP 恢复、过程和索引 DDL。 | Main operator manual. Use [README_EN.md](README_EN.md#operator-guide) for English command navigation. |
| [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md) | Test plan | Chinese | 完整链路测试方案，覆盖导出、导入、数据比对、索引和存储过程重建。 | Full end-to-end test plan. See [BIC_DMDUL_E2E_TEST_PLAN_EN.md](BIC_DMDUL_E2E_TEST_PLAN_EN.md) for English summary. |
| [BIC_DMDUL_E2E_TEST_PLAN_EN.md](BIC_DMDUL_E2E_TEST_PLAN_EN.md) | Test plan index | English | 完整链路测试方案英文索引。 | English summary and pointer for the full E2E test plan. |
| [DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md) | Storage summary | Chinese | 最新 DM8 文件、页、行、LOB、字典、恢复模式总结。 | Latest storage-format summary. See [README_EN.md](README_EN.md#storage-architecture-and-format) for English overview. |
| [DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md](DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md) | Design note | Chinese | bootstrap 字典下载和标准表下载路径设计。 | Bootstrap dictionary download and standard table extraction design. |
| [DM8_STORAGE_ARCHITECTURE_NOTES.md](DM8_STORAGE_ARCHITECTURE_NOTES.md) | Deep research note | English | 存储架构深度笔记，记录大量原始证据和假设。 | Deep storage architecture research note with evidence and hypotheses. |
| [DM8_PAGE_STRUCTURE_NOTES.md](DM8_PAGE_STRUCTURE_NOTES.md) | Deep research note | English | 页结构字段、页头、行槽和未知区域记录。 | Page header, slot, row, and candidate-field notes. |
| [DM8_TYPE_STORAGE_NOTES.md](DM8_TYPE_STORAGE_NOTES.md) | Deep research note | English | 字段类型物理存储证据。 | Type storage evidence and decoding rules. |
| [DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md](DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md) | Calibration note | English | `dump()` 在线校准与离线解码对齐记录。 | Online `dump()` calibration and offline decoder alignment. |
| [EVIDENCE_CAPTURE_WORKFLOW.md](EVIDENCE_CAPTURE_WORKFLOW.md) | Workflow | English | 证据采集、复现和验证流程。 | Evidence capture, reproducibility, and validation workflow. |
| [PROJECT_GOAL.md](PROJECT_GOAL.md) | Goal | English | 项目目标和正确性约束。 | Project goal and correctness constraints. |
| [TECHNICAL_EXPLORATION_ROADMAP.md](TECHNICAL_EXPLORATION_ROADMAP.md) | Roadmap | English | 技术探索路线图。 | Technical exploration roadmap. |
| [EXPLORATION_PLAN.md](EXPLORATION_PLAN.md) | Plan | English | 存储探索计划。 | Storage exploration plan. |
| [EXPLORATION_TASKS.md](EXPLORATION_TASKS.md) | Checklist | English | 探索与实现任务清单。 | Exploration and implementation checklist. |
| [FOUNDATIONAL_RESEARCH_PLAN.md](FOUNDATIONAL_RESEARCH_PLAN.md) | Plan | English | 基础研究门禁计划。 | Foundational storage research gate. |
| [STORAGE_EXPLORATION_2026-06-29.md](STORAGE_EXPLORATION_2026-06-29.md) | Historical note | English | 2026-06-29 首次现场探索记录。 | First live storage exploration record. |
| [TEST_ENVIRONMENT.md](TEST_ENVIRONMENT.md) | Environment | English | 测试环境说明。 | Test environment description. |
| [templates/evidence_manifest.json](templates/evidence_manifest.json) | Template | JSON | 证据 manifest 模板。 | Evidence manifest template. |
| [../NOTICE.md](../NOTICE.md) | Legal notice | Bilingual | 开源版权声明，包含中英文开发者名称和 GPL 协议说明。 | Open source copyright notice with Chinese/English developer names and GPL license statement. |
| [../LICENSE](../LICENSE) | License | English | GPL-3.0 完整协议文本。 | Full GPL-3.0 license text. |

## Bilingual Maintenance Rules / 双语维护规则

- New user-facing documents should provide both Chinese and English summaries.
- Keep historical evidence notes stable: do not rename files that are referenced by code, tests, commits, or previous reports.
- Prefer adding bilingual navigation and summaries over rewriting raw evidence notes.
- When a document becomes a primary operator document, add it to both [README_CN.md](README_CN.md) and [README_EN.md](README_EN.md).

---

Copyright (C) 2026 佰晟智算（深圳）技术有限公司 / Baisheng Intelligent Computing (Shenzhen) Co., Ltd.

Website: https://www.dbaiops.com

License: GPL-3.0-or-later.
