# bic-dmdul

`bic-dmdul` 是佰晟智算（深圳）技术有限公司开发的达梦 DM8 离线数据抽取工具，定位类似 Oracle DUL。

`bic-dmdul` is a DM8 offline data extraction tool developed by Baisheng Intelligent Computing (Shenzhen) Co., Ltd. Its role is similar to Oracle DUL.

官方网站 / Website: [www.dbaiops.com](https://www.dbaiops.com)

当 DM 数据库实例无法启动、但数据文件或 ASM 磁盘组仍可读取时，`bic-dmdul` 的目标是直接从底层存储中恢复系统字典、定位用户表存储对象，并导出表数据、LOB 附件和可重装载 SQL。

When a DM database instance cannot start but the data files or ASM disk groups are still readable, `bic-dmdul` is designed to recover dictionary metadata, locate table storage objects, and export table rows, LOB attachments, and reloadable SQL directly from storage.

## License

项目名称：`bic-dmdul`

版权所有 (C) 2026 佰晟智算（深圳）技术有限公司。

开发者：佰晟智算（深圳）技术有限公司 / Baisheng Intelligent Computing (Shenzhen) Co., Ltd.

官方网站：[www.dbaiops.com](https://www.dbaiops.com)

本项目采用 GNU General Public License 第 3 版或后续版本（`GPL-3.0-or-later`）开源。你可以依照 GPL 协议的条款重新发布和/或修改本程序，完整协议文本见 [LICENSE](LICENSE)。

`bic-dmdul` 是数据库灾难拯救工具，故障场景可能导致无法无损恢复；关于数据损失、恢复不完整或恢复错误的免责说明见 [NOTICE.md](NOTICE.md)。

本程序按“原样”发布，不提供任何担保，包括但不限于适销性或特定用途适用性的默示担保。完整开源版权声明见 [NOTICE.md](NOTICE.md)。

## Command

安装后主命令为：

```bash
bic-dmdul --help
```

为了兼容早期脚本，当前仍保留 `dmdul` 命令别名；新文档和新脚本应使用 `bic-dmdul`。

开发环境中也可以继续使用内部 Python 模块入口：

```bash
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m dmdul.cli --help
```

## Documentation / 文档入口

Start here when browsing the Git project:

- [Documentation Home / 文档总入口](docs/README.md)
- [中文文档索引](docs/README_CN.md)
- [English Documentation Index](docs/README_EN.md)
- [中文使用手册](docs/USER_MANUAL_CN.md)
- [Open Source Copyright Notice](NOTICE.md)

The documentation set is organized by audience:

| Need | 中文入口 | English Entry |
| --- | --- | --- |
| Install, bootstrap, dump, import, recovery commands | [中文使用手册](docs/USER_MANUAL_CN.md) | [English command guide in docs index](docs/README_EN.md#operator-guide) |
| Full export/import/compare validation | [完整链路测试方案](docs/BIC_DMDUL_E2E_TEST_PLAN_CN.md) | [E2E test plan summary](docs/BIC_DMDUL_E2E_TEST_PLAN_EN.md) |
| Current DM8 storage conclusions | [DM8 存储格式阶段性总结](docs/DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md) | [Storage documents map](docs/README_EN.md#storage-architecture-and-format) |
| Continue development with AI coding tools | [AI Coding 二次开发指南](docs/AI_CODING_DEVELOPMENT_GUIDE.md) | [AI coding development guide](docs/AI_CODING_DEVELOPMENT_GUIDE.md) |
| Research tasks and roadmap | [中文索引：研究与路线图](docs/README_CN.md#研究路线图与任务) | [English index: roadmap and tasks](docs/README_EN.md#roadmap-and-tasks) |
| Evidence capture and validation | [中文索引：证据与校准](docs/README_CN.md#证据校准与测试环境) | [English index: evidence and calibration](docs/README_EN.md#evidence-calibration-and-test-environment) |

## Main Capabilities / 主要能力

- 从离线 DM8 数据文件中解析控制文件、表空间、数据文件、页、段、BTREE 表和行数据。
- 通过 `SYSTEM.DBF` 中的 SYS 字典表 bootstrap 出 `user.dict`、`tab.dict`、`col.dict`、`file.dict` 等离线字典。
- 当 SYSTEM 或核心字典缺失时，可显式使用 storage scan 模式扫描数据文件，生成 `storage_scan.dict` 和 `SCAN.TAB_<storage_id>` 占位表。
- 支持 DUL 文本、raw-safe row archive、LOB 附件、`STORAGE(USING LONG ROW)` 超长变长列、分区表、并发导出、TRUNCATE/DROP 后 storage 级恢复。
- 按需生成用户存储过程和索引创建脚本。
- 已验证一种压缩 `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` 恢复路径：定向 bootstrap 可保留 `$AUX/$RAUX/$DAUX/$UAUX` 辅助对象，导出时通过 `$RAUX` BTREE storage 恢复逻辑行，并完成导入后双向 `MINUS=0/0` 比对。
- 严格模式会传播不完整恢复风险，例如 `$RAUX` 代理映射会产生 `huge-raux-proxy-mapping`，`dump-data --strict` 会通过 `tables_strict_failed` 和非零退出码提示尚不能证明完整恢复。
- 已补充 [AI Coding 二次开发指南](docs/AI_CODING_DEVELOPMENT_GUIDE.md)，用于指导 Codex、Claude Code、Hermes、Trae、Qoder 等工具继续开发。

## Current Limitations / 当前遗留问题

- ASM 磁盘组读取尚未实现。
- 缺失 `SYSTEM.DBF` 时，storage scan 可以保留 raw 行和候选 storage，但不能自动恢复真实 owner、表名、列名和完整字段类型。
- `QUERY HIGH`、列级压缩、带分区或 LOB 的压缩 HUGE 表需要实现 `$AUX.CPR_FLAG='Y'` 列区解压和行重组后才能宣称完整恢复。
- 真正跨块行链、active slot 指向专用行迁移指针的格式仍需更多样本验证；当前已支持 `STORAGE(USING LONG ROW)` 的 `0x22` out-of-line 变长列，并按 slot 目录跳过不活动旧物理行。
- 未提交事务、异常崩溃中间态、完整 MVCC/UNDO 可见性仍需单独研究。
- 直接连接目标 DM 库执行并发导入尚未实现；当前导入工具生成可审计 SQL。

## Bootstrap Example

```bash
TMPDIR=./tmp bic-dmdul \
  --page-size 8192 bootstrap /path/to/offline/dbcopy \
  --output-dir tmp/bootstrap-dicts -b --json
```

`bootstrap -b` 会扫描 `SYSTEM.DBF` 并写出第一阶段离线字典。没有系统字典时，不会静默猜测业务表结构；需要显式使用灾难恢复扫描模式。
