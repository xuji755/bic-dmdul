# AI Coding Development Guide / AI Coding 二次开发指南

This guide is for engineers who use AI coding tools such as Codex, Claude Code, Hermes, Trae, Qoder, or similar agents to continue developing `bic-dmdul`.

本文面向使用 Codex、Claude Code、Hermes、Trae、Qoder 等 AI Coding 工具继续二次开发 `bic-dmdul` 的工程师。

## 1. Project Positioning / 项目定位

`bic-dmdul` is a DM8 offline disaster-recovery extraction tool. Its goal is to recover table data when the DM instance cannot start but files are still readable. Correctness is more important than optimistic extraction.

`bic-dmdul` 是达梦 DM8 离线灾难恢复抽取工具。目标是在数据库实例无法启动、但数据文件仍可读取时恢复表数据。正确性优先于“尽量导出”。

Key rule:

核心原则：

- Online views such as `DBA_TABLES`, `DBA_SEGMENTS`, `DBA_EXTENTS`, `DBA_SOURCE`, and `DBA_INDEXES` may be used only for calibration and testing.
- Final recovery logic must read offline files, control-file evidence, SYS dictionary tables, downloaded dict files, row archives, LOB pages, and storage structures.
- If a structure is not fully understood, the tool must report uncertainty through diagnostics and strict-mode failures instead of silently claiming success.

## 2. Recommended AI Tool Workflow / 推荐 AI 工具工作流

When starting a new AI coding session, paste or reference this checklist first:

开启新的 AI Coding 会话时，先给工具提供以下约束：

1. Work in the repository root.
   在仓库根目录工作。

   ```bash
   cd /home/loop/dmdul
   ```

2. Read the project entry documents before editing code.
   修改代码前先阅读项目入口文档。

   ```bash
   sed -n '1,180p' README.md
   sed -n '1,220p' docs/README.md
   sed -n '1,140p' docs/PROJECT_GOAL.md
   sed -n '1,220p' docs/DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md
   ```

3. Run Python code with `PYTHONPATH=src`.
   本地运行 Python 命令时使用 `PYTHONPATH=src`。

   ```bash
   TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m unittest discover -s tests
   TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m dmdul.cli --help
   ```

4. Keep temporary files under `tmp/`.
   临时文件放在 `tmp/` 下。

5. Use small, focused changes.
   每次改动保持聚焦，不做无关重构。

## 3. Repository Map / 仓库结构

| Path | Purpose / 用途 |
| --- | --- |
| `src/dmdul/cli.py` | CLI command definitions, command orchestration, copyright banner. 命令行入口和命令编排。 |
| `src/dmdul/bootstrap.py` | Offline dictionary bootstrap, SYS dictionary parsing, storage scanning. 离线字典下载、SYS 字典解析和 storage 扫描。 |
| `src/dmdul/metadata.py` | Dict-file metadata loading, table/storage mapping, HUGE `$RAUX` proxy mapping. 离线字典元数据装配。 |
| `src/dmdul/extract.py` | Page planning, row extraction, strict diagnostics, DUL/row output. 页规划、行导出和严格模式诊断。 |
| `src/dmdul/decode.py` | DM row value decoding. 行字段解码。 |
| `src/dmdul/lob.py` | Out-of-line LOB locator and page-chain reading. LOB 页链读取。 |
| `src/dmdul/row_archive.py` | Raw-safe `.row` archive writer. raw-safe row archive 写出。 |
| `tests/` | Unit and CLI regression tests. 单元测试和 CLI 回归测试。 |
| `docs/` | User manual, storage notes, test plans, evidence workflow. 使用手册、存储结构、测试方案和证据流程。 |
| `tmp/` | Ignored scratch area for local temporary outputs. 本地临时输出目录。 |

## 4. Development Rules For AI Agents / AI Agent 开发规则

AI tools should follow these rules:

AI 工具必须遵守：

- Do not remove diagnostics just to make tests pass.
- Do not convert uncertain recovery into successful recovery without export, import, and compare evidence.
- Do not use `sample_limit` or partial sampling as a correctness proof.
- Do not infer duplicate tables by name only; owner/user and partition objects matter.
- Do not re-scan SYS dictionaries after bootstrap when `dict` files already contain downloaded metadata, unless explicitly testing bootstrap itself.
- Do not depend on online `DBA_*` views in final extraction commands.
- Do not commit passwords, tokens, host credentials, raw customer data, or generated test outputs.
- Do not use destructive git operations such as `git reset --hard` unless a human explicitly requests it.

Preferred behavior:

推荐行为：

- Preserve raw evidence when decoding is incomplete.
- Add strict-mode diagnostics for incomplete or risky paths.
- Add tests before or with parser changes.
- Update Chinese and English documentation when behavior changes.
- Use real end-to-end validation for user-visible recovery features.

## 5. Correctness Gates / 正确性门禁

For normal parser or CLI changes:

普通解析器或命令行改动：

```bash
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m unittest discover -s tests
```

For a new extraction feature, the minimum acceptance chain is:

新增抽取能力的最低验收链路：

1. Create or identify a controlled source table.
   创建或定位受控源表。
2. Bootstrap offline dictionaries.
   离线 bootstrap 字典。
3. Export with `dump-data`.
   使用 `dump-data` 导出。
4. Import into a different target user, usually `DMDUL_RT` or a test user.
   导入到不同目标用户，通常是 `DMDUL_RT` 或测试用户。
5. Compare source and target with count, aggregates, and bidirectional `MINUS`.
   使用行数、聚合、双向 `MINUS` 比对源目标。
6. Save the command summary in docs or evidence notes when the case expands support boundaries.
   如果该用例扩展了支持边界，需要把命令摘要写入文档或证据记录。

A command that only writes a file is not enough evidence.

只生成导出文件不能证明功能正确。

## 6. Remote DM8 Lab Usage / 远端 DM8 实验环境使用

Remote experiments should be repeatable and isolated.

远端实验应可复现、可隔离。

General pattern:

通用模式：

```bash
tar -C /home/loop/dmdul -cz src tests pyproject.toml | \
  ssh dmdba@192.168.32.102 \
  'BASE=/home/dmdba/dmdul/tmp/<case_name>; rm -rf "$BASE/code" && mkdir -p "$BASE/code" && tar -xz -C "$BASE/code"'
```

Then run:

然后执行：

```bash
cd /home/dmdba/dmdul/tmp/<case_name>/code
PY38=/opt/gbase/python3.8/bin/python3.8
PYTHONPATH=src "$PY38" -m dmdul.cli --help
```

Remote rules:

远端规则：

- Use `/home/dmdba/dmdul/tmp/<case_name>` or `/opt/dmdul/tmp`, not random system directories.
- Keep source test objects under explicit test names such as `DMDUL_*`.
- Do not modify production-looking objects.
- Use online SQL only to calibrate and compare; do not make the offline tool rely on online views.
- When testing `disql`, set `LD_LIBRARY_PATH=/opt/dmdbms/bin:$LD_LIBRARY_PATH`.

## 7. High-Risk Feature Notes / 高风险功能注意事项

### 7.1 SYS Dictionary Bootstrap / SYS 字典 Bootstrap

Bootstrap exists to avoid repeated full scans. After `tab.dict`, `col.dict`, `user.dict`, and `file.dict` are generated, later dump commands should read dict files instead of scanning dictionary tables again.

bootstrap 的目的就是避免反复全库扫描。生成 `tab.dict`、`col.dict`、`user.dict`、`file.dict` 后，后续导出命令应使用 dict 文件。

When adding dictionary support:

新增字典支持时：

- Decode real row fields; do not infer object type from object name.
- Use `TYPE$` or equivalent decoded dictionary fields for object type.
- Handle same table names under different owners.
- Handle partition and subpartition dictionary rows explicitly.

### 7.2 Row And Type Decoding / 行与类型解码

Field decoding must restore values from physical row bytes without lossy name-based guessing.

字段解码必须从物理行 bytes 还原值，不能依赖名称猜测。

When adding a type decoder:

新增类型解码时：

- Create test tables covering nulls, boundary values, updates, and multi-row pages.
- Compare exported/imported values with the original table.
- Include raw hex evidence for unclear encodings.

Special row forms:

特殊行形态：

- In-code `scan_observed_row_chain` means the physical in-page row-length sequence. It does not mean cross-block row chaining.
- `STORAGE(USING LONG ROW)` is supported for the verified DM8 shape: row metadata state `01`, a 21-byte locator in the variable-column payload, and `0x22` long-row data pages.
- Row chaining means one logical row is too long for one block and is split across multiple data blocks. This is different from `USING LONG ROW`; it still requires pointer decoding and payload reassembly before claiming complete recovery.
- Row migration means an updated row moved to another block while the original location keeps a pointer or old physical bytes. Rows outside the page slot directory must not be exported as live rows; if an active slot points to a migration pointer, report or implement pointer skip logic before claiming success.
- AI agents must not treat a successful partial decode of the first row piece as complete data. Add diagnostics and strict-mode failures until the chained-row format is verified.

### 7.3 LOB / LOB 字段

LOB recovery must preserve payload bytes.

LOB 恢复必须保持 payload bytes。

Current supported paths include short inline LOB, out-of-line LOB page chains, and verified `USING LONG ROW` out-of-line variable-column payloads. If a locator cannot be followed, write locator evidence and report a diagnostic instead of returning an invented value.

当前已支持短内联 LOB、out-of-line LOB 页链，以及已验证的 `USING LONG ROW` out-of-line 变长列 payload。无法解析 locator 时，应写出 locator 证据并报告诊断，不能伪造值。

### 7.4 Partitioned Tables / 分区表

Partition support must export all leaf partitions by default. Single or list partition export must filter by explicit leaf partition names.

分区表默认应导出所有叶子分区。单分区或 LIST 导出必须按明确的叶子分区名过滤。

Do not treat a partitioned table's parent object as the only storage entry.

不要把分区表父对象当作唯一 storage 入口。

### 7.5 TRUNCATE And DROP Recovery / TRUNCATE 与 DROP 恢复

TRUNCATE/DROP recovery is storage-id based and must be marked as recovery mode. It may scan orphan or old storage ids, but any recovered table must still be importable and comparable when source or snapshot evidence exists.

TRUNCATE/DROP 恢复基于 storage id，必须明确标记为恢复模式。它可以扫描孤儿或旧 storage id，但只要有源表或快照证据，仍必须导入并比对。

### 7.6 Compressed HUGE Tables / 压缩 HUGE 表

HUGE tables are column-store structures. The main table can show `ROOTFILE=-1` and `ROOTPAGE=-1`; data may live in hidden auxiliary objects and huge tablespace files.

HUGE 表是列存结构。主表可能显示 `ROOTFILE=-1`、`ROOTPAGE=-1`；数据可能位于隐藏辅助对象和 huge tablespace 文件中。

Current facts:

当前事实：

- `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` has one verified path through `$RAUX`, with import/compare evidence.
- `$RAUX` proxy mapping is marked by `huge-raux-proxy-mapping`.
- In strict mode, this diagnostic is treated as uncertainty.
- `QUERY HIGH` can produce `$AUX.CPR_FLAG='Y'` compressed column sections.
- These column sections live under paths such as `HMAIN/SCH<schema_id>/TAB<object_id>/COLxxxx_*.dta`.
- Variable-length column sections can contain zlib streams; after decompression, the first 4096 bytes are 1024 little-endian end-offsets, followed by concatenated values.

Required next step before claiming full `QUERY HIGH` support:

宣称完整支持 `QUERY HIGH` 前必须完成：

1. Discover huge tablespace paths from control/system metadata.
2. Map table object id to `TAB<object_id>` huge directory.
3. Read `COL*.dta` section headers.
4. Decode `$AUX` section metadata.
5. Decompress and decode each column by type.
6. Reassemble rows by section row number.
7. Export, import, and compare all rows.

## 8. Documentation Rules / 文档规则

When behavior changes, update docs in both languages where applicable.

行为变化时，应同步更新中英文文档。

Minimum documentation targets:

最低文档更新目标：

- User-visible commands: update [USER_MANUAL_CN.md](USER_MANUAL_CN.md).
- Storage-format conclusions: update [DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md](DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md).
- Test scope changes: update [BIC_DMDUL_E2E_TEST_PLAN_CN.md](BIC_DMDUL_E2E_TEST_PLAN_CN.md) and [BIC_DMDUL_E2E_TEST_PLAN_EN.md](BIC_DMDUL_E2E_TEST_PLAN_EN.md).
- New primary documents: add links to [README.md](README.md), [README_CN.md](README_CN.md), and [README_EN.md](README_EN.md).

Do not rewrite historical evidence notes just to make them cleaner. Add new sections or cross references instead.

不要为了“整理”重写历史证据笔记。优先追加新章节或交叉引用。

## 9. Commit Checklist / 提交前检查清单

Before committing AI-generated changes:

提交 AI 生成的改动前：

```bash
git status --short
git diff --stat
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m unittest discover -s tests
```

Then review:

然后检查：

- No credentials, tokens, or test passwords are added to tracked files.
- No generated dump files, `.row` archives, LOB payloads, or large evidence files are staged accidentally.
- New diagnostics are documented.
- Strict mode fails when recovery is incomplete or uncertain.
- User-visible behavior has tests.
- Documentation links are updated.

## 10. Suggested Prompt For AI Tools / 推荐给 AI 工具的提示词

Chinese prompt:

中文提示词：

```text
你正在 /home/loop/dmdul 开发 bic-dmdul。请先阅读 README.md、docs/README.md、docs/PROJECT_GOAL.md、docs/DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md 和 docs/AI_CODING_DEVELOPMENT_GUIDE.md。所有 Python 命令使用 TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src。不要依赖在线 DBA_* 视图实现离线恢复逻辑；在线视图只能用于校准。任何不完整恢复必须通过 diagnostics 和 strict_ok=false 表达。改动后运行 python3 -m unittest discover -s tests，并同步更新中英文文档索引。
```

English prompt:

英文提示词：

```text
You are developing bic-dmdul in /home/loop/dmdul. Read README.md, docs/README.md, docs/PROJECT_GOAL.md, docs/DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md, and docs/AI_CODING_DEVELOPMENT_GUIDE.md before editing. Run Python commands with TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src. Do not implement offline recovery logic that depends on online DBA_* views; online views are calibration only. Any incomplete recovery must be reported through diagnostics and strict_ok=false. After changes, run python3 -m unittest discover -s tests and update Chinese/English documentation indexes.
```

---

Copyright (C) 2026 佰晟智算（深圳）技术有限公司 / Baisheng Intelligent Computing (Shenzhen) Co., Ltd.

Website: https://www.dbaiops.com

License: GPL-3.0-or-later.
