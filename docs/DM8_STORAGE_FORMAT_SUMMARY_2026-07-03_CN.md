# DM8 存储格式阶段性总结 - 2026-07-03

本文汇总最近两天在 `dmdul` 项目中已经验证的达梦 DM8 离线抽取相关结论。目标是给后续实现和排错提供一份高密度参考。这里的结论以 DBF 原始字节、离线抽取结果、在线 SQL 校准三方一致为准；未完全证明的字段仍按“工作命名”记录。

## 1. 总体原则

- 最终工具不能依赖 `DBA_*` 视图；视图只用于在线校准。
- `bootstrap` 负责扫描 `SYSTEM.DBF` 并下载字典，生成 `file.dict`、`tab.dict`、`col.dict`。
- `dump-data --dict-dir` 只能使用已经下载的 dict 文件，不应反复扫描 `SYSOBJECTS`、`SYSCOLUMNS`、`SYSINDEXES`。
- 表数据抽取必须从当前活动行出发。特别是 LOB：旧 LOB 页可能残留在 DBF 中，不能靠扫描全部 LOB 页导出。

## 2. 数据文件与页头

当前测试库页大小为 `8192` 字节。`DMDUL_TS01.DBF` 的 group/tablespace id 为 `6`，file hint 为 `0`。

通用页头前 64 字节已观察到稳定字段：

| 偏移 | 长度 | 当前工作名 | 说明 |
| ---: | ---: | --- | --- |
| `0x00` | 4 | `group_raw` | 低 16 位为 group id，高 16 位为 file hint |
| `0x04` | 4 | `page_no` | 文件内页号 |
| `0x08` | 6 | `prev_page_ref` | `u16 file_no + u32 page_no`，全 `ff` 表示空 |
| `0x0e` | 6 | `next_page_ref` | 同上 |
| `0x14` | 4 | `page_kind_raw` | 当前用于识别页角色 |

常见 `page_kind_raw`：

| 值 | 当前含义 |
| ---: | --- |
| `0x14` | 普通表 BTREE leaf/data 页 |
| `0x15` | BTREE root/internal 或 segment root 类页 |
| `0x20` | LOB 数据页 |
| `0x23` | 空间/LOB 相关元数据页，未完全解析 |
| `0xffff00ff` | 已初始化空页 |
| `0x1a1a001a` | 内部元数据页 |

文件前若干页用于文件头、空间管理和控制结构。新建业务表空间中普通对象通常从页 16 之后开始分配。

## 3. 字典与段定位

核心字典表：

| SYS 表 | 用途 |
| --- | --- |
| `SYS.SYSOBJECTS` | 对象名、对象 id、schema id、类型、父对象 |
| `SYS.SYSCOLUMNS` | 列名、列序号、类型、长度、scale、nullable |
| `SYS.SYSINDEXES` | 表 storage object 的 group/file/root page 等 |

当前 DM8 测试库已能从 `SYSTEM.DBF` 发现并下载这些字典：

| 字典表 | storage id | file | root page |
| --- | ---: | ---: | ---: |
| `SYSOBJECTS` | `33554540` | `0` | `16` |
| `SYSINDEXES` | `33554434` | `0` | `288` |
| `SYSCOLUMNS` | `33554433` | `0` | `80` |

`SYSOBJECTS` 普通 slot 行中已验证的关键布局：

```text
row.data[7:11]   ID
row.data[11:15]  SCHID
row.data[15:19]  PID，0xffffffff 表示无父对象
variable area    NAME, TYPE$, SUBTYPE$
```

对象类型必须使用 `TYPE$` / `SUBTYPE$` 判断，不能靠对象名猜测。普通用户表为 `SCHOBJ/UTAB`。表的内部 storage object 为 `TABOBJ/INDEX`，其 `PID` 指向表对象或分区叶子对象。

同名表必须按 owner/schema 区分，判断重复不能只看表名。

## 4. 表与分区定位

普通表：

```text
SYSOBJECTS:  表对象 SCHOBJ/UTAB
SYSOBJECTS:  子对象 TABOBJ/INDEX
SYSINDEXES:  storage id, group id, root file, root page
```

分区表：

```text
父表:          SCHOBJ/UTAB, PID=-1
中间分区:      SCHOBJ/UTAB, PID=<父表或父分区 id>
叶子分区:      SCHOBJ/UTAB, PID=<父分区 id>
数据 storage: TABOBJ/INDEX, PID=<叶子分区对象 id>
```

导出表数据时只使用叶子分区/子分区对应的 `TABOBJ/INDEX` storage root。中间分区对象不是实际行数据入口。

已经验证的复杂分区：

| 表 | 叶子页 | 行数 |
| --- | --- | ---: |
| `DMDUL_PART_LIST` | `1424,1440,1456` | 3 |
| `DMDUL_PART_HASH` | `1488,1504,1520,1536` | 4 |
| `DMDUL_PART_RANGE_HASH` | `1568,1584,1600,1616` | 4 |

`bootstrap` 会把分区表所有叶子 root 写入 `tab.dict.page_refs`，后续 `dump-data` 直接按这些 page refs 导出。

### 4.1 压缩 HUGE 表定位

已验证达梦压缩 `HUGE TABLE` 与普通 BTREE 表段不同。测试语法：

```sql
CREATE HUGE TABLE SYSDBA.DMDUL_HUGE_COMP_T (
  ID INT,
  K INT,
  C2 CHAR(2),
  V VARCHAR(64),
  PAD VARCHAR(1000)
) COMPRESS LEVEL 1 FOR 'QUERY LOW';
```

在线字典表现：

- `DBA_TABLES.COMPRESSION = ENABLED`；
- 主表 `DBA_SEGMENTS.HEADER_FILE=-1`、`HEADER_BLOCK=-1`、`BYTES=0`；
- `SYSOBJECTS` 中出现主表和内部辅助表：
  - `DMDUL_HUGE_COMP_T$AUX`
  - `DMDUL_HUGE_COMP_T$RAUX`
  - `DMDUL_HUGE_COMP_T$DAUX`
  - `DMDUL_HUGE_COMP_T$UAUX`

当前验证结论：

- 主表对象保存逻辑列定义；
- `$RAUX` 辅助表保存真实逻辑行，列结构与主表一致；
- `$RAUX` 有普通 BTREE storage root，可按现有 page plan 导出；
- `dmdul` 离线装配元数据时，如果主表没有普通 storage、同 owner 存在 `主表名$RAUX` 且主表有列定义，会使用主表列定义 + `$RAUX` storage 作为主表导出入口。

验证结果：

| 表 | 类型 | 行数 | 导出结果 | 导入比对 |
| --- | --- | ---: | --- | --- |
| `SYSDBA.DMDUL_HUGE_COMP_T` | `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` | 5000 | `rows_written=5000`, `decode_error=0` | 导入 `DMTEST.DMDUL_HUGE_COMP_T_RT` 后双向 `MINUS=0` |

边界：

- 普通 `CREATE TABLE ... COMPRESS` 在当前测试库中没有让 `DBA_TABLES.COMPRESSION` 变为 `ENABLED`；
- `COMPRESS_MODE=1` 能设置参数值，但普通表仍显示 `COMPRESSION=DISABLED`；
- `QUERY HIGH`、列级压缩、带分区或 LOB 的压缩 HUGE 表尚未验证。

## 5. BTREE 表页与 page plan

达梦普通表以 BTREE 组织存储。当前抽取策略：

1. 从 dict 得到 storage root。
2. 如果 root 页是 `0x14`，直接按 leaf/data 页处理。
3. 如果 root 页是 `0x15`，从 internal/root 页下降到最左 leaf，并校验 root entry 覆盖关系。
4. 沿 data 页 `next_page` 链导出。
5. 每页校验 page identity、page kind 和 storage id。
6. 对多 extent 表，不假设 extent 连续；按 BTREE leaf 链或 storage-id 页计划处理。

已验证 `DMDUL_EXT2` 超过 1 个 extent，25,000 行严格导出成功。`DBA_EXTENTS` 在当前实例对该段只返回一行，不能作为完整 leaf 页计划依赖。

## 6. 行结构

普通行目前已验证的基本模型：

```text
2 bytes row length/status
metadata bytes, 大小约 ceil(column_count / 4)
fixed-width columns, 按 storage order
variable-width columns, 按 storage order
row tail/control bytes
```

SQL 列顺序仍按字典列序输出，但物理上固定宽度列在前、变长列在后。

行头：

- 前 2 字节为 big-endian 长度/status。
- 高位表示删除行。提交删除后的物理行仍可能残留，但应跳过。

NULL metadata：

- 每个 storage-order 列占 2 bit，小端读取。
- `00` 表示非 NULL。
- `11` 表示 NULL。
- fixed-width NULL 列仍占用其固定宽度字节。
- variable-width NULL 列不写 length prefix 和 payload。

页尾 slot：

- 页头 `0x2c` 处观察到 active row/slot count。
- 页尾有 2 字节 row offset 数组，抽取时优先按 slot 找活动行。
- physical row chain 可能包含 deleted/old row，因此不能只按物理连续行全部导出。

row tail/control：

- 当前样本中普通行尾控制区约 19 字节。
- DML 后该区域会变化，疑似包含事务/undo/版本链信息。
- MVCC/未提交事务可见性尚未完整解析；当前验证集中在已提交状态。

## 7. 变长字段编码

普通变长字段 length prefix：

```text
0x80..0xff  => 单字节长度，长度 = byte - 0x80
0x00..0x7f  => 双字节 big-endian 长度
```

该编码同时出现在用户表普通 VARCHAR/LOB locator payload 和 SYS 字典行中的字符串字段。

## 8. 数据类型存储

已实现并通过远端校准的普通字段类型：

```text
BIGINT, BINARY, BLOB, BYTE, CHAR, CLOB, DATE, DATETIME,
DATETIME WITH TIME ZONE, DEC, DECIMAL, DOUBLE, FLOAT, INT, INTEGER,
INTERVAL DAY TO SECOND, NUMBER, NUMERIC, REAL, ROWID, SMALLINT, TEXT,
TIME, TIME WITH TIME ZONE, TIMESTAMP, TIMESTAMP WITH LOCAL TIME ZONE,
TIMESTAMP WITH TIME ZONE, TINYINT, VARBINARY, VARCHAR, VARCHAR2
```

主要编码：

| 类型 | 页内编码 |
| --- | --- |
| `TINYINT` | 1 字节 little-endian signed |
| `SMALLINT` | 2 字节 little-endian signed |
| `INT/INTEGER` | 4 字节 little-endian signed |
| `BIGINT` | 8 字节 little-endian signed |
| `REAL` | 4 字节 IEEE754 little-endian |
| `FLOAT` | 字典 length 为 4 时 4 字节，否则 8 字节 |
| `DOUBLE` | 8 字节 IEEE754 little-endian |
| `BYTE` | 1 字节，导出为 hex |
| `BINARY/VARBINARY` | 变长 bytes，导出为 hex |
| `CHAR/VARCHAR/VARCHAR2` | 变长字符 bytes，ASCII/GB18030/UTF-8 解码 |
| `NUMBER/NUMERIC/DEC/DECIMAL` | 变长 base-100，零为 `0x80` |
| `DATE` | 3 字节 packed date |
| `TIME` | 5 字节 packed time，含 microsecond |
| `TIMESTAMP/DATETIME` | 8 字节，`DATE(3)+TIME(5)` |
| `TIME WITH TIME ZONE` | 7 字节，`TIME(5)+tz offset(2)` |
| `TIMESTAMP/DATETIME WITH TIME ZONE` | 10 字节，`TIMESTAMP(8)+tz offset(2)` |
| `TIMESTAMP WITH LOCAL TIME ZONE` | 8 字节 timestamp |
| `INTERVAL DAY TO SECOND` | 24 字节，前 5 个 int32 为 day/hour/min/sec/usec |
| `ROWID` | 12 字节，DM 显示为三段 4 字节 big-endian 的 base64 风格编码 |

DM8 测试库非 ASCII 字符当前按 GB18030 家族存储。解码策略是 ASCII fast path，其次 GB18030，再 UTF-8 fallback。

## 9. LOB 存储

### 9.1 短内联 LOB

`TEXT/CLOB/BLOB` 短值可能在行内 payload 中带 13 字节前缀：

```text
00      flag，当前观察到 0x01
01..08  locator/control bytes
09..12  inline payload length, little-endian
13..    inline payload
```

当 `inline payload length == remaining bytes` 时，抽取器去掉前缀，只导出真实 payload。

### 9.2 out-of-line LOB locator

大 LOB 行内存 21 字节 locator：

```text
00      flag，0x02 表示 out-of-line LOB
01..04  LOB id, little-endian
05..08  当前未命名控制字段
09..12  source byte length, little-endian
13..16  group id, little-endian
17..20  first LOB data page, little-endian
```

示例：

```text
DOC: 0260d4060000000000606d00000600000086060000
BIN: 0261d4060000000000e02e0000060000008b060000
```

其中 `0x6d60=28000` 是 CLOB 原始 GB18030 字节数，`0x2ee0=12000` 是 BLOB 字节数。

### 9.3 LOB 数据页

LOB 数据页 `page_kind_raw=0x20`。当前已验证字段：

| 偏移 | 长度 | 含义 |
| ---: | ---: | --- |
| `0x08` | 6 | prev page ref |
| `0x0e` | 6 | next page ref |
| `0x14` | 4 | `0x20` |
| `0x24` | 4 | LOB id |
| `0x2c` | 2 | 当前页 payload length |
| `0x38` | variable | payload bytes 起点 |

读取规则：

1. 从当前活动行 locator 取得 LOB id、source bytes、group id、首页。
2. 校验每个 LOB 页的 group、file hint、page_no、page kind、LOB id。
3. 按 `next_page` 串联 payload。
4. 读满 locator 中的 source byte length 后停止。
5. `BLOB` 输出原始 bytes。
6. `CLOB/TEXT` 按字符集解码后写 UTF-8 附件，manifest 记录 `source_bytes` 和输出 `bytes`。

### 9.4 LOB 更新与旧版本

已验证更新场景：

```text
insert OLD_LOB_一_... / cafebabe...
commit
update to NEW_LOB_二_... / deadbeef...
commit
```

在线 SQL 和离线 `dump-data` 都只返回 NEW 版本。原始 DBF 仍能扫描到旧 LOB 页：

```text
old CLOB pages: 1766 -> 1767 -> 1768
old BLOB pages: 1769 -> 1770
new CLOB pages: 1771 -> 1772 -> 1773
new BLOB pages: 1774 -> 1775
current row page: 1776
```

结论：旧 LOB 物理页可能残留，但不能作为当前表数据导出。必须先解析当前活动行，再只跟随该行 locator。

## 10. 输出格式

默认 DUL/CSV 文本格式中，LOB 外置：

```text
SYSDBA.T.dul
SYSDBA.T.lob/
  00000001/DOC.clob
  00000001/BIN.blob
  manifest.jsonl
```

主 DUL/CSV 文件中写：

```text
@LOB:SYSDBA.T.lob/00000001/DOC.clob
```

manifest 字段包括 table、row_sequence、column、type_name、status、file、bytes、sha256；out-of-line LOB 还记录 pages；文本转码时记录 source_encoding/source_bytes/output_encoding。

新增 row 归档格式面向大表和跨服务器重装载：

```sh
dmdul dump-data --dict-dir dulout --output-dir dumpout --table SYSDBA.T --output-format row
dmdul import-data --input dumpout/SYSDBA.T.row --output-sql dumpout/SYSDBA.T.import.sql
```

`.row` 是二进制文件，不使用 JSON 承载行数据。文件内包含：

- owner、表名、列定义；
- 建表脚本；
- 每条活动行的 file/page/row offset；
- 每条活动行的原始 DM 行内 bytes；
- 该行引用的 LOB payload blocks。

row 归档可以脱离原始 DBF 复制到另一台服务器。`import-data` 默认输出文件内建表脚本，再按归档中的行 bytes 解码生成 `INSERT`；`--no-create-table` 可跳过建表，`--table` 可指定导入目标表名。

`import-data` 支持两种输入结构：

- DUL 文本：读取文件头部 `CREATE TABLE` 和 `-- DATA` 后的分隔符数据；遇到 `@LOB:<relative-path>` 时读取同目录下的 `.lob/` 附件。
- row 归档：读取二进制头部中的 `CREATE TABLE`、列定义、原始行内 bytes 和内嵌 LOB payload blocks。

`import-row` 保留为兼容别名，但后续文档和流程统一使用 `import-data`。

## 11. 当前边界

- SYSTEM.DBF 缺失后的全文件扫描重组尚不是主流程。
- ASM 磁盘组读取尚未实现。
- 未提交事务、异常崩溃中间态、回滚未清理版本仍需单独实验。
- row tail/control 与完整 MVCC/undo 可见性尚未完全解析。
- 已验证压缩 `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` 可通过 `$RAUX` storage 导出；其他压缩形态、加密、特殊迁移行、跨文件 LOB 链尚未覆盖。
