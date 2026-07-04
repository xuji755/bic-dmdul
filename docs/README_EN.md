# bic-dmdul English Documentation Index

This page organizes the full `bic-dmdul` documentation set for English readers. Some operator-facing and summary documents are written in Chinese because the project is driven by a Chinese DM8 recovery workflow; this index provides English reading guidance for every document and keeps original filenames stable for evidence traceability.

Website: [www.dbaiops.com](https://www.dbaiops.com)

## Quick Start

- [Documentation home](README.md): bilingual documentation hub.
- [Project goal](PROJECT_GOAL.md): correctness target and scope.
- [Storage architecture notes](DM8_STORAGE_ARCHITECTURE_NOTES.md): deep storage evidence.
- [Page structure notes](DM8_PAGE_STRUCTURE_NOTES.md): page and row layout notes.
- [AI coding development guide](AI_CODING_DEVELOPMENT_GUIDE.md): workflow for continuing development with Codex, Claude Code, Hermes, Trae, Qoder, or similar tools.
- [Chinese open-source announcement draft](BLOG_BIC_DMDUL_OPEN_SOURCE_CN.md): blog draft for announcing `bic-dmdul`.
- [Open source copyright notice](../NOTICE.md): GPL and developer notice.
- [GPL license text](../LICENSE): full GPL-3.0 license.

## Operator Guide

| Document | English Reading Guide |
| --- | --- |
| [USER_MANUAL_CN.md](USER_MANUAL_CN.md) | Main operator manual. It covers `prepare`, `bootstrap`, `dump-data`, `import-data`, partition export, LOB attachments, TRUNCATE/DROP recovery, procedure DDL, and index DDL. Command examples are directly usable even if the prose is Chinese. |
| [BLOG_BIC_DMDUL_OPEN_SOURCE_CN.md](BLOG_BIC_DMDUL_OPEN_SOURCE_CN.md) | Chinese blog draft announcing the open-source `bic-dmdul` tool, including purpose, capabilities, license, limitations, and community direction. |
| [BIC_DMDUL_E2E_TEST_PLAN_EN.md](BIC_DMDUL_E2E_TEST_PLAN_EN.md) | English summary of the full E2E test plan. The detailed executable design is in [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md). |
| [TEST_ENVIRONMENT.md](TEST_ENVIRONMENT.md) | Test host, DM8 instance, data-file locations, and exploration environment. |

## Storage Architecture And Format

| Document | English Reading Guide |
| --- | --- |
| [DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md) | Current high-density storage-format summary. It covers control files, tablespaces, pages, rows, LOBs, dictionaries, no-SYSTEM storage scan, and TRUNCATE/DROP recovery. |
| [DM8_STORAGE_ARCHITECTURE_NOTES.md](DM8_STORAGE_ARCHITECTURE_NOTES.md) | Deep architecture notes with page numbers, storage ids, root pages, dictionary evidence, partition evidence, and current boundaries. |
| [DM8_PAGE_STRUCTURE_NOTES.md](DM8_PAGE_STRUCTURE_NOTES.md) | Page-level research note: page headers, page kinds, row slots, row scanning, and candidate unknown fields. |
| [DM8_TYPE_STORAGE_NOTES.md](DM8_TYPE_STORAGE_NOTES.md) | Type-storage evidence for numeric, character, temporal, binary, and LOB values. |
| [DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md](DM8_DUMP_TYPE_CALIBRATION_2026-07-01.md) | Online DM `dump()` calibration and the matching offline decoder behavior. |

## Bootstrap Dictionary And Offline Download

| Document | English Reading Guide |
| --- | --- |
| [DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md](DM8_BOOTSTRAP_STANDARD_TABLE_DOWNLOAD.md) | Design note for normal bootstrap and table download. It explains why the standard path must start from dictionary storage roots instead of repeated full-file scans. |
| [PROJECT_GOAL.md](PROJECT_GOAL.md) | User-facing goal, correctness model, and the rule that final extraction cannot depend on online `DBA_*` views. |

## Roadmap And Tasks

| Document | English Reading Guide |
| --- | --- |
| [AI_CODING_DEVELOPMENT_GUIDE.md](AI_CODING_DEVELOPMENT_GUIDE.md) | Bilingual guide for AI-assisted second-stage development. It covers context loading, remote experiments, test gates, strict diagnostics, compressed HUGE boundaries, and commit checks. |
| [TECHNICAL_EXPLORATION_ROADMAP.md](TECHNICAL_EXPLORATION_ROADMAP.md) | Technical roadmap from cold-consistent extraction toward crash-state extraction. |
| [EXPLORATION_PLAN.md](EXPLORATION_PLAN.md) | Database-level storage exploration plan. |
| [EXPLORATION_TASKS.md](EXPLORATION_TASKS.md) | Exploration and implementation checklist. |
| [FOUNDATIONAL_RESEARCH_PLAN.md](FOUNDATIONAL_RESEARCH_PLAN.md) | Research gate for explaining row visibility and byte-to-value mapping before adding extraction behavior. |
| [STORAGE_EXPLORATION_2026-06-29.md](STORAGE_EXPLORATION_2026-06-29.md) | First live storage exploration note, useful for tracing early assumptions. |

## Evidence, Calibration, And Test Environment

| Document | English Reading Guide |
| --- | --- |
| [EVIDENCE_CAPTURE_WORKFLOW.md](EVIDENCE_CAPTURE_WORKFLOW.md) | Workflow for converting a DM8 storage question into reproducible evidence. |
| [templates/evidence_manifest.json](templates/evidence_manifest.json) | Evidence manifest template. |

## Chinese Entry

Chinese readers should use [README_CN.md](README_CN.md). The bilingual documentation hub is [README.md](README.md).
