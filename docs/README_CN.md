# bic-dmdul 中文文档索引

本页面向中文读者，按使用场景组织 `bic-dmdul` 的全部技术文档。部分历史探索文档使用英文记录，表格中提供中文阅读说明；这些文档保留原名，是为了保护证据链和历史引用。

官方网站：[www.dbaiops.com](https://www.dbaiops.com)

## 快速入口

- [中文使用手册](USER_MANUAL_CN.md)：日常命令入口，优先阅读。
- [DM8 存储格式阶段性总结](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md)：当前最重要的存储结构总结。
- [Bootstrap 与标准表下载优化设计](DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md)：离线字典与表下载主路径。
- [开源版权声明](../NOTICE.md)：GPL 和开发者声明。
- [GPL 协议全文](../LICENSE)：GPL-3.0 完整协议文本。

## 操作手册

| 文档 | 内容 |
| --- | --- |
| [USER_MANUAL_CN.md](USER_MANUAL_CN.md) | `bic-dmdul` 中文使用手册，覆盖 `prepare`、`bootstrap`、`dump-data`、`import-data`、分区导出、LOB 附件、TRUNCATE/DROP 恢复、过程和索引 DDL。 |
| [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md) | 完整链路测试方案，要求导出、导入、目标用户重建和源目标比对，覆盖索引和存储过程。 |
| [TEST_ENVIRONMENT.md](TEST_ENVIRONMENT.md) | 测试环境、DM8 实例、数据文件位置和研究背景。 |

## DM8 存储结构

| 文档 | 内容 |
| --- | --- |
| [DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md) | 当前最新阶段性总结，覆盖控制文件、表空间、页、行、LOB、字典、无 SYSTEM 字典扫描、TRUNCATE/DROP 恢复。 |
| [DM8_STORAGE_ARCHITECTURE_NOTES.md](DM8_STORAGE_ARCHITECTURE_NOTES.md) | 存储架构深度笔记，包含大量页号、storage id、root page、字典和分区证据。 |
| [DM8_PAGE_STRUCTURE_NOTES.md](DM8_PAGE_STRUCTURE_NOTES.md) | 页结构笔记，记录页头、页类型、行槽、页内行扫描和未知字段。 |
| [DM8_TYPE_STORAGE_NOTES.md](DM8_TYPE_STORAGE_NOTES.md) | 字段类型存储笔记，记录数字、字符、日期时间、LOB 等类型证据。 |
| [DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md](DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md) | 使用 DM 在线 `dump()` 结果校准离线解码器的记录。 |

## Bootstrap、字典与下载路径

| 文档 | 内容 |
| --- | --- |
| [DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md](DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md) | 标准 bootstrap 和表下载设计，说明为什么正常路径应使用 storage root，而不是反复全文件扫描。 |
| [PROJECT_GOAL.md](PROJECT_GOAL.md) | 项目目标、正确性目标和不依赖在线 `DBA_*` 视图的约束。 |

## 研究路线图与任务

| 文档 | 内容 |
| --- | --- |
| [TECHNICAL_EXPLORATION_ROADMAP.md](TECHNICAL_EXPLORATION_ROADMAP.md) | 技术路线图：从冷一致文件抽取到崩溃状态抽取的目标拆分。 |
| [EXPLORATION_PLAN.md](EXPLORATION_PLAN.md) | 数据库级存储探索计划。 |
| [EXPLORATION_TASKS.md](EXPLORATION_TASKS.md) | 探索和实现任务清单，记录已完成和待完成项。 |
| [FOUNDATIONAL_RESEARCH_PLAN.md](FOUNDATIONAL_RESEARCH_PLAN.md) | 基础研究门禁计划，强调解释每个字节再实现解析器。 |
| [STORAGE_EXPLORATION_2026-06-29.md](STORAGE_EXPLORATION_2026-06-29.md) | 首次现场探索记录，适合追溯早期假设来源。 |

## 证据、校准与模板

| 文档 | 内容 |
| --- | --- |
| [EVIDENCE_CAPTURE_WORKFLOW.md](EVIDENCE_CAPTURE_WORKFLOW.md) | 证据采集流程，说明如何把 DM8 存储问题转成可复现实验。 |
| [templates/evidence_manifest.json](templates/evidence_manifest.json) | 证据 manifest 模板。 |

## 英文阅读入口

英文读者请使用 [README_EN.md](README_EN.md)。双语总入口见 [README.md](README.md)。
