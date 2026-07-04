# bic-dmdul 完整链路测试方案

本文定义 `bic-dmdul` 当前功能的系统级测试方案。目标不是只证明命令能运行，而是证明“源库构造 -> 离线字典 bootstrap -> 离线导出 -> 目标用户导入 -> 数据/对象比对”的完整链路可验证、可重复、可审计。

## 1. 测试目标

核心目标：

- 验证普通表、复杂类型表、LOB 表、分区表、大表、压缩 HUGE 表、TRUNCATE/DROP 恢复场景可以完成离线导出。
- 验证导出的 DUL/row/parts 文件可以生成 SQL 并导入到目标用户。
- 验证导入后目标表与源表数据一致，至少做到行数、双向 `MINUS`、关键聚合、LOB 附件校验一致。
- 验证 `dump-procedures` 导出的存储过程脚本可以在目标用户重建并执行。
- 验证 `dump-indexes` 导出的普通 BTree/唯一索引脚本可以在目标用户重建，索引列、唯一性、可用状态与源对象一致。
- 验证异常场景不会伪造成功：复杂索引、未知 LOB locator、缺失文件、页损坏、无法解析字段必须报告诊断或跳过原因。

## 2. 测试环境

建议固定使用两类用户：

| 用户 | 用途 |
| --- | --- |
| `SYSDBA` 或 `DMDUL_SRC` | 源对象构造用户。 |
| `DMTEST` 或 `DMDUL_RT` | 目标导入验证用户。 |

建议固定目录：

| 目录 | 用途 |
| --- | --- |
| `/opt/dmdul` | 远端发布包路径。 |
| `/dmdata/data/DAMENG` | 源 DM8 数据文件目录。 |
| `/home/dmdba/dmdul/tmp/e2e_<run_id>` | 每次测试独立工作目录。 |
| `<work>/dict` | bootstrap 字典目录。 |
| `<work>/dump` | 离线导出结果目录。 |
| `<work>/ddl` | 过程和索引 DDL 输出目录。 |
| `<work>/import_sql` | `import-data` 生成的 SQL。 |
| `<work>/compare` | 比对 SQL、结果和日志。 |

每次测试必须记录：

- `bic-dmdul --version` 输出。
- Git commit id。
- DM 版本。
- 数据文件复制方式：干净关闭、checkpoint 后在线复制、存储快照或其他。
- `bootstrap_manifest.json`。
- 每个 `dump-data` 的 JSON/report 输出。
- 每个导入 SQL 的执行日志。
- 每个比对 SQL 的结果。

## 3. 总体测试流程

所有正向场景都使用同一条链路：

1. 清理并创建源 schema、目标 schema、测试表空间。
2. 创建测试表、索引、存储过程并插入确定性数据。
3. 执行 `checkpoint(0)`，复制或固定数据文件快照。
4. 执行 `bic-dmdul prepare` 生成 `init.dul` 和 `filelist.dul`。
5. 执行 `bic-dmdul bootstrap -b` 生成离线字典。
6. 执行 `bic-dmdul dump-data` 导出表数据。
7. 执行 `bic-dmdul import-data` 把 `.dul`、`.row` 或 parts manifest 生成导入 SQL。
8. 在目标用户执行导入 SQL。
9. 执行 `bic-dmdul dump-procedures` 和 `bic-dmdul dump-indexes`。
10. 对过程和索引 DDL 做 owner/table 重写后在目标用户执行。
11. 对数据、LOB、索引、存储过程执行比对。
12. 收集所有日志和 JSON 报告，输出总测试报告。

## 4. 源数据设计

### 4.1 标量类型覆盖表

表名：`DMDUL_E2E_TYPES`

字段建议：

| 列 | 类型 | 覆盖点 |
| --- | --- | --- |
| `ID` | `INT` | 主键、排序和比对键。 |
| `C_TINY` | `TINYINT` | 小整数边界。 |
| `C_SMALL` | `SMALLINT` | 负数、0、正数。 |
| `C_INT` | `INT` | 32 位边界。 |
| `C_BIG` | `BIGINT` | 64 位整数。 |
| `C_NUM` | `NUMBER(38,10)` | 高精度十进制。 |
| `C_DEC` | `DECIMAL(18,4)` | 定点小数。 |
| `C_CHAR` | `CHAR(10)` | 定长补空格。 |
| `C_VC` | `VARCHAR(3000)` | 变长阈值：1、127、128、255、256、1000、3000。 |
| `C_BIN` | `VARBINARY(256)` | 二进制字节、0x00、0xff。 |
| `C_DATE` | `DATE` | 日期边界。 |
| `C_TIME` | `TIME(6)` | 微秒。 |
| `C_TS` | `TIMESTAMP(6)` | 时间戳微秒。 |
| `C_TMTZ` | `TIME(6) WITH TIME ZONE` | 带时区时间精度。 |
| `C_TSTZ` | `TIMESTAMP(6) WITH TIME ZONE` | 带时区时间戳。 |
| `C_NULLABLE` | `VARCHAR(50)` | NULL bitmap。 |

数据规模：

- 边界值 20 行。
- 典型值 1000 行。
- NULL 组合 50 行。

通过标准：

- 导出行数等于源表行数。
- 导入目标表后双向 `MINUS` 为 0。
- 时间带时区列不得丢失合法精度。
- `VARBINARY` 使用 hex 或 raw-safe 格式比对。

### 4.2 大表与多 extent 表

表名：`DMDUL_E2E_BIG`

字段：

- `ID INT`
- `BUCKET INT`
- `MARKER VARCHAR(64)`
- `PAD VARCHAR(3000)`

数据规模：

- 至少 50,000 行。
- 必须超过 1 个 extent。
- `PAD` 使用确定性公式生成，例如 `RPAD('PAD_' || ID, 1000 + MOD(ID, 2000), '#')`。

通过标准：

- `COUNT(*)` 一致。
- `MIN(ID)`、`MAX(ID)`、`SUM(ID)` 一致。
- 按 `BUCKET` 分组的 `COUNT(*)`、`SUM(ID)` 一致。
- 随机抽样 100 行按主键逐列一致。
- 双向 `MINUS` 为 0；如果 DM 对超长字符显示有差异，使用规范化比较 SQL。

### 4.3 LOB 表

表名：`DMDUL_E2E_LOB`

字段：

- `ID INT`
- `DOC CLOB`
- `BIN BLOB`
- `NOTE VARCHAR(200)`

数据规模和覆盖点：

- 短内联 CLOB/BLOB。
- 大 CLOB：中文、英文、换行、分隔符、超过一页。
- 大 BLOB：包含 0x00、0xff、重复块、随机块。
- LOB 更新场景：插入后更新一次 LOB，再 checkpoint。
- LOB NULL 场景。

导出要求：

- 默认使用 LOB 附件目录。
- 记录 `manifest.jsonl`。
- CLOB 记录原始编码、source bytes、输出 bytes、sha256。
- BLOB 记录 sha256。

通过标准：

- 主表行数一致。
- 非 LOB 列双向 `MINUS` 为 0。
- 每个非 NULL LOB 的 `length`、`sha256` 与源库查询结果一致。
- 导入目标表后，目标 LOB 的 `length`、`hash` 与源表一致。

### 4.4 分区表

覆盖三类表：

| 表 | 分区类型 | 场景 |
| --- | --- | --- |
| `DMDUL_E2E_PART_RANGE` | RANGE | 按日期或 ID 分区。 |
| `DMDUL_E2E_PART_LIST` | LIST | 国家/类型列表分区。 |
| `DMDUL_E2E_PART_RANGE_HASH` | RANGE-HASH 二级分区 | leaf subpartition 全扫描。 |

数据规模：

- 每个 leaf partition/subpartition 至少 50 行。
- 每个分区放入可识别 marker。

测试命令：

- 全表导出。
- 指定单个分区导出。
- 指定多个分区 LIST 导出。
- `--partition-parallel > 1` 表内并发导出。

通过标准：

- 全表导入后与源表双向 `MINUS=0/0`。
- 单分区导入后仅包含该分区源数据。
- 多分区 LIST 导入后仅包含指定分区集合。
- parts manifest 中列出的 part 文件完整存在，导入工具可自动找到并导入。

### 4.5 压缩 HUGE 表

表名：`DMDUL_E2E_HUGE_COMP`

建表形态：

```sql
CREATE HUGE TABLE DMDUL_E2E_HUGE_COMP (
  ID INT,
  MARKER VARCHAR(64),
  PAD VARCHAR(2000)
) COMPRESS LEVEL 1 FOR 'QUERY LOW';
```

数据规模：

- 至少 10,000 行。

当前已验证状态：

- 2026-07-04 全面测试使用既有 `SYSDBA.DMDUL_HUGE_COMP_T` 验证，在线行数为 5000；
- 主表 storage 子对象为 `INDEX33596000`，`ROOTFILE=-1`、`ROOTPAGE=-1`，不能直接作为普通表入口；
- `$RAUX` 辅助表 storage 子对象为 `INDEX33596002`，`GROUPID=4`、`ROOTFILE=0`、`ROOTPAGE=949488`；
- `bootstrap --table` 会 fallback 到 SYSTEM 扫描，写出主表和 `$AUX/$RAUX/$DAUX/$UAUX` 辅助表行；
- `dump-data` 用主表列定义 + `$RAUX` storage 导出主表逻辑数据。
- 2026-07-04 追加 `SYSDBA.DMDUL_HUGE_HIGH_T` 测试：`HUGE TABLE ... STORAGE(SECTION(1024)) COMPRESS LEVEL 9 FOR 'QUERY HIGH'`，在线行数 20000，`$AUX` 有 100 条列区元数据，其中 95 条 `CPR_FLAG=Y`，`$RAUX` 只有 544 行。当前工具不能完整恢复该形态，`dump-data --strict` 必须返回 `strict_ok=false` 和 `tables_strict_failed=1`。

通过标准：

- `bic-dmdul` 通过 `$RAUX` storage 自动导出主表逻辑数据。
- 导入目标表后 `COUNT`、聚合和双向 `MINUS` 一致。
- 报告中不能出现行解码错误。
- 对含 `huge-raux-proxy-mapping` 的报告，不能只凭导出成功判定通过；必须导入比对成功，或在 `--strict` 下明确作为未完整验证处理。

### 4.6 TRUNCATE 恢复表

表名：`DMDUL_E2E_TRUNC`

流程：

1. 插入 10,000 行。
2. bootstrap 并保存 `dict_before`。
3. 记录 `tab.dict.storage_index_id/storage_index_ids`。
4. 执行 `TRUNCATE TABLE DMDUL_E2E_TRUNC`。
5. checkpoint 并复制数据文件。
6. 使用 truncate 恢复模式导出。

通过标准：

- `dump-data --truncate` 不需要人工提供 storage id。
- 对普通表和分区表均能读取对应 storage id 列表。
- 恢复出的行数与 truncate 前一致，双向比对使用 truncate 前源数据快照表。

### 4.7 DROP 恢复表

表名：`DMDUL_E2E_DROP`

覆盖两种场景：

- `SYSOBJECTS` 中仍能找到删除标记或历史记录。
- 当前字典中找不到表记录，只能扫描 orphan storage。

流程：

1. 插入确定性数据。
2. 保存 drop 前字典。
3. DROP 表。
4. checkpoint 并复制数据文件。
5. 使用 drop 前字典按旧 storage id 恢复。
6. 使用当前字典执行 `scan-orphan-storages`。
7. 对 orphan 候选使用字段列表适配并导出。
8. 无字段列表时验证 raw 导出和推测 schema 报告。

通过标准：

- 有字段列表时导入目标表并与 drop 前快照双向 `MINUS=0/0`。
- 无字段列表时导出 raw 行，不声称逻辑列完全恢复。
- orphan scan 支持按表空间过滤和默认全库扫描。

## 5. 存储过程重建测试

### 5.1 源过程设计

测试过程名：`DMDUL_E2E_PROC_SUMMARY`

目标：

- 使用类似 Oracle PL/SQL 的语法风格。
- 覆盖参数、变量、`SELECT ... INTO`、条件分支、异常路径、中文字符串。
- 依赖一张业务表，导入后可在目标用户执行并验证输出。

建议源码：

```sql
CREATE OR REPLACE PROCEDURE DMDUL_E2E_PROC_SUMMARY(
  P_MIN_ID IN INT,
  P_ROW_COUNT OUT INT,
  P_TOTAL_ID OUT BIGINT,
  P_MESSAGE OUT VARCHAR(200)
)
AS
BEGIN
  SELECT COUNT(*), COALESCE(SUM(ID), 0)
    INTO P_ROW_COUNT, P_TOTAL_ID
    FROM DMDUL_E2E_BIG
   WHERE ID >= P_MIN_ID;

  IF P_ROW_COUNT > 0 THEN
    P_MESSAGE := '达梦恢复验证OK:' || CAST(P_ROW_COUNT AS VARCHAR(30));
  ELSE
    P_MESSAGE := 'NO DATA';
  END IF;
END;
/
```

如果当前 DM 语法对 `COALESCE`、`CAST`、`||` 或 `CREATE OR REPLACE PROCEDURE` 有差异，以远端 DM8 实测语法为准，但必须保留：

- 输入参数。
- 输出参数。
- `SELECT ... INTO`。
- 变量赋值。
- 条件分支。
- 中文字符串。

### 5.2 导出与重建

命令：

```sh
bic-dmdul dump-procedures \
  --dict-dir <work>/dict \
  --owner DMDUL_SRC \
  --output <work>/ddl/DMDUL_SRC.procedures.sql \
  --json
```

重建要求：

- 将 owner/table 引用重写到目标用户，例如 `DMDUL_RT.DMDUL_E2E_BIG`。
- 在目标用户执行过程 DDL。
- 查询系统视图确认过程存在且有效。
- 执行过程并比对输出。

通过标准：

- 源过程源码完整导出，不丢失中文、换行、分号和 `/` 分隔符。
- 目标过程编译成功。
- 源过程与目标过程在相同参数下输出一致。

示例比对：

```sql
-- 源用户执行，记录输出
CALL DMDUL_SRC.DMDUL_E2E_PROC_SUMMARY(100, ?, ?, ?);

-- 目标用户执行，记录输出
CALL DMDUL_RT.DMDUL_E2E_PROC_SUMMARY(100, ?, ?, ?);
```

如果 `CALL` 绑定 OUT 参数在 `disql` 中不方便自动化，可补充一个包装函数或结果表：

```sql
CREATE TABLE DMDUL_E2E_PROC_RESULT (
  RUN_SIDE VARCHAR(10),
  ROW_COUNT INT,
  TOTAL_ID BIGINT,
  MESSAGE VARCHAR(200)
);
```

过程测试时把输出写入结果表，再比较源目标结果表。

## 6. 索引重建测试

### 6.1 源索引设计

在源表上创建：

```sql
CREATE UNIQUE INDEX DMDUL_E2E_TYPES_U1 ON DMDUL_E2E_TYPES(ID);
CREATE INDEX DMDUL_E2E_BIG_I1 ON DMDUL_E2E_BIG(BUCKET, ID);
CREATE INDEX DMDUL_E2E_TYPES_I2 ON DMDUL_E2E_TYPES(C_VC);
```

可选负向对象：

- bitmap index。
- function-based index。
- virtual/cluster 相关索引。

负向对象用于验证 `dump-indexes` 报告 skipped，不得伪造 DDL。

### 6.2 导出与重建

命令：

```sh
bic-dmdul dump-indexes \
  --dict-dir <work>/dict \
  --owner DMDUL_SRC \
  --output <work>/ddl/DMDUL_SRC.indexes.sql \
  --json
```

重建要求：

- 目标表数据导入完成后执行索引 DDL。
- owner 和表名改写到目标用户。
- 普通 BTree 和 unique index 必须重建。
- 复杂未支持索引必须出现在 `skipped` 中。

通过标准：

- 目标用户索引数量与预期一致。
- 索引列顺序一致。
- unique index 唯一性有效：插入重复 `ID` 应失败。
- 对 `WHERE BUCKET=? AND ID=?` 的查询，执行计划或系统视图能看到目标索引存在并可用。

## 7. 导出格式覆盖

每类表至少覆盖：

| 格式 | 场景 |
| --- | --- |
| DUL 文本 | 小表、普通表、人工可读验证。 |
| row archive | 大表、复杂类型表、raw-safe 重装载。 |
| parts manifest | 分区表或大表并发导出。 |
| LOB 附件 | CLOB/BLOB 表。 |
| raw orphan DUL | DROP 后无字段列表的 orphan storage 恢复。 |

通过标准：

- `import-data` 支持 DUL、row、parts 两种主结构和 parts 子文件。
- row 格式头部包含建表脚本。
- DUL 和 row 导入结果一致。
- LOB 附件路径在导入时可解析。

## 8. 比对方法

### 8.1 通用表比对

每张表必须执行：

```sql
SELECT COUNT(*) FROM SRC_TABLE;
SELECT COUNT(*) FROM RT_TABLE;

SELECT COUNT(*) FROM (
  SELECT <cols> FROM SRC_TABLE
  MINUS
  SELECT <cols> FROM RT_TABLE
);

SELECT COUNT(*) FROM (
  SELECT <cols> FROM RT_TABLE
  MINUS
  SELECT <cols> FROM SRC_TABLE
);
```

通过标准：

- count 相等。
- 双向 `MINUS` 都为 0。

### 8.2 大表补充比对

```sql
SELECT MIN(ID), MAX(ID), COUNT(*), SUM(ID) FROM T;
SELECT BUCKET, COUNT(*), SUM(ID) FROM T GROUP BY BUCKET ORDER BY BUCKET;
```

通过标准：

- 聚合结果完全一致。
- 抽样主键逐列一致。

### 8.3 LOB 比对

建议在源库和目标库分别生成：

| 字段 | 含义 |
| --- | --- |
| `ID` | 主键。 |
| `DOC_LEN` | CLOB 字符或字节长度。 |
| `DOC_HASH` | CLOB 规范化字节 hash。 |
| `BIN_LEN` | BLOB 字节长度。 |
| `BIN_HASH` | BLOB 字节 hash。 |

通过标准：

- 每个 ID 的长度和 hash 一致。
- 附件 manifest 中 sha256 与导入后数据库 hash 一致。

如果 DM SQL 内置 hash 函数不足，使用 `disql` 导出 LOB 到文件后在 OS 层计算 `sha256sum`。

## 9. 并发测试

覆盖：

- 多表并发导出：`--workers 4`、`--workers 8`。
- 分区表表内并发：`--partition-parallel 4`。
- 每个 worker 写独立文件和子目录。
- 导入端按 parts manifest 并发生成或执行 SQL。

通过标准：

- 并发导出结果与单线程导出结果一致。
- 没有文件覆盖、part 丢失、LOB 附件路径冲突。
- report 汇总中 `tables_failed=0`。

## 10. 负向和诊断测试

| 场景 | 预期 |
| --- | --- |
| 缺失某个 DBF 文件 | 命令失败或报告 fatal diagnostic，不输出伪造成功。 |
| 页头 storage id 不匹配 | 跳过该页并记录诊断。 |
| 随机破坏某个数据页副本 | 报告 decode/page diagnostic；未损坏页仍可导出时必须记录部分成功边界。 |
| 未支持复杂索引 | `dump-indexes --json` 中 `skipped` 有明确 reason。 |
| 无法解析 LOB locator | 输出 `.locator.hex`，记录 `lob-locator-not-followed`。 |
| 无字段列表 DROP orphan 恢复 | 只输出 raw 行和推测 schema 报告，不声明逻辑列全恢复。 |

## 11. 自动化脚本设计

建议实现一个远端测试 driver：

```sh
tests/e2e/run_dm8_e2e.sh <run_id>
```

或者 Python driver：

```sh
PYTHONPATH=src python3 tests/e2e/run_dm8_e2e.py --run-id <run_id>
```

模块划分：

| 模块 | 职责 |
| --- | --- |
| `00_env_check` | 检查 DM 连接、bic-dmdul 版本、目录权限。 |
| `10_create_source` | 建源用户、目标用户、表空间、源表、索引、过程、数据。 |
| `20_snapshot` | checkpoint、复制数据文件或记录在线文件路径。 |
| `30_bootstrap` | prepare/bootstrap 并校验 dict 文件。 |
| `40_dump_data` | 按场景导出 DUL/row/parts/LOB/raw。 |
| `50_import_data` | 生成导入 SQL，执行到目标用户。 |
| `60_dump_ddl` | dump-procedures/dump-indexes，重写 owner 后导入。 |
| `70_compare` | 执行 count、MINUS、hash、过程输出、索引检查。 |
| `80_report` | 汇总 JSON/Markdown 报告。 |

## 12. 总通过标准

一次完整测试通过必须同时满足：

- `bootstrap` 成功，核心 dict 文件存在且行数非 0。
- 所有正向表导出 `ok=true`。
- 所有正向表导入目标用户成功。
- 所有正向表 count 和双向 `MINUS` 通过。
- LOB 长度和 hash 通过。
- 分区导出、并发导出、parts manifest 导入通过。
- TRUNCATE 恢复与 truncate 前快照一致。
- DROP 有字段列表恢复与 drop 前快照一致；无字段列表场景只做 raw 恢复声明。
- 存储过程 DDL 重建成功，并且执行输出与源过程一致。
- 普通索引和唯一索引重建成功，列顺序和唯一性验证通过。
- 负向场景有明确 diagnostic 或 skipped reason。
- 所有测试产物归档到 `<work>/report`，包含命令、日志、JSON、SQL、比对结果和 commit id。

## 13. 第一阶段执行清单

第一阶段先完成这些场景：

- `DMDUL_E2E_TYPES`：DUL 和 row 双格式导出导入比对。
- `DMDUL_E2E_BIG`：50,000 行，多 extent，row 格式导出导入比对。
- `DMDUL_E2E_LOB`：CLOB/BLOB 附件导出导入和 sha256 比对。
- `DMDUL_E2E_PART_RANGE_HASH`：全表导出、指定分区导出、parts 并发导出。
- `DMDUL_E2E_TRUNC`：普通表和分区表 truncate 恢复。
- `DMDUL_E2E_DROP`：有字段列表 orphan 恢复。
- `DMDUL_E2E_PROC_SUMMARY`：过程导出、重建、执行输出比对。
- `DMDUL_E2E_TYPES_U1`、`DMDUL_E2E_BIG_I1`：索引导出、重建、唯一性和列顺序验证。

第一阶段不要求覆盖：

- 崩溃恢复中的未提交事务可见性。
- ASM 磁盘组。
- 加密表。
- 所有复杂索引类型重建。
- `QUERY HIGH` 的完整列压缩区恢复；当前只验证到 `$AUX.CPR_FLAG='Y'` 场景会触发严格失败。
- 列级压缩、带分区或 LOB 的 HUGE 压缩表。
- 所有未知压缩形态。
