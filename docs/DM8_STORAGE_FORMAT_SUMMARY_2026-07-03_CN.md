# DM8 存储格式阶段性总结 - 2026-07-04

本文汇总 `bic-dmdul` 项目中已经验证的达梦 DM8 离线抽取相关结论。目标是给后续实现和排错提供一份高密度参考。这里的结论以 DBF 原始字节、离线抽取结果、在线 SQL 校准和导入后比对为准；未完全证明的字段仍按“工作命名”记录。

## 1. 总体原则

- 最终工具不能依赖 `DBA_*` 视图；视图只用于在线校准。
- 正常 `bootstrap -b` 负责从 `SYSTEM.DBF` 下载字典，生成 `file.dict`、`user.dict`、`tab.dict`、`col.dict` 和空的 `storage_scan.dict`。
- 如果 `SYSTEM.DBF` 或关键 SYS 字典不可用，普通 `bootstrap -b` 必须报错并返回非 0；不能把空字典当成正常恢复字典。
- 显式 `bootstrap --scan-storages-without-system-dicts` 是降级恢复模式，不访问 SYS 字典，扫描所有 DBF 页头并生成 `storage_scan.dict` 与 `SCAN.TAB_<storage_id>` 占位对象。
- `dump-data --dict-dir` 只能使用已经下载的 dict 文件，不应反复扫描 `SYSOBJECTS`、`SYSCOLUMNS`、`SYSINDEXES`。
- `dump-data --scan-storage-dict` 只用于无 SYS 字典扫描模式，按 `SCAN.TAB_<storage_id>` 导出完整物理行 bytes 到 `raw_row`，不伪造列定义。
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

控制文件 `dm.ctl` 中保存数据文件到表空间的关系。当前测试库中可从控制文件字符串结构观察到：

- 表空间名字符串在同一控制记录中先出现；
- 部分记录中间有 `NORMAL` 这类属性字符串；
- 随后出现一个或多个 DBF 路径；
- 多文件表空间如 `MAIN` 对应 `MAIN.DBF/main2.dbf/main3.dbf`；
- `DMDUL_TS01.DBF` 的控制文件映射为表空间 `DMDUL_TS`，不应从文件名推断。

因此 `file.dict.tablespace_name` 应来自控制文件解析。`scan-orphan-storages --tablespace` 使用这个字段过滤数据文件；如果旧 dict 没有该字段，应重新 bootstrap 或退回使用 `--group-id`。

无系统字典扫描模式下，`storage_scan.dict` 记录：

| 字段 | 含义 |
| --- | --- |
| `storage_id` | 页头 `0x3a` 处的 storage id |
| `group_id/file_no/path/page_size` | 数据文件定位 |
| `pages` | 命中的 `page_kind_raw=0x14` 数据页数 |
| `page_refs` | 完整 `file_no:page_no` 页清单 |
| `first_pages` | 前若干候选页，便于人工检查 |
| `row_samples` | 样本行 raw hex、offset、长度、ASCII hint |
| `kind_counts` | 同一 storage id 观察到的页类型计数 |

该模式不能恢复 owner、真实表名、列名和字段类型；它的目标是先聚合 storage 并保留物理行，为后续人工确认或 `recover-orphan-table --column ...` 结构化导出提供入口。

## 3. 字典与段定位

核心字典表：

| SYS 表 | 用途 |
| --- | --- |
| `SYS.SYSOBJECTS` | 对象名、对象 id、schema id、类型、父对象 |
| `SYS.SYSCOLUMNS` | 列名、列序号、类型、长度、scale、nullable |
| `SYS.SYSINDEXES` | 表 storage object 的 group/file/root page 等 |
| `SYS.SYSTEXTS` | 存储过程/函数等对象源码，按需导出过程 DDL 时读取 |

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

当前按需 DDL 支持：

- `dump-procedures`：执行时离线扫描 `SYSOBJECTS` 和 `SYSTEXTS`，按 owner 输出过程源码，`SYSTEXTS.TXT` 的 CLOB 支持内联和 out-of-line LOB，中文源码按 ASCII/GB18030/UTF-8 路径解码。
- `dump-indexes`：执行时离线扫描 `SYSOBJECTS`、`SYSINDEXES`，优先使用已有 `col.dict` 映射列名；已支持普通 BTree 索引和唯一索引 DDL。`SYSINDEXES.KEYNUM` 当前按 `u16 context[23:25]` 解析，`KEYINFO` 按变长字段定位，普通组合索引每列 3 字节条目：`u16 column_id + order_marker`。
- 未验证的 cluster、virtual、bitmap、function-based 等复杂索引只报告 skipped，不输出伪造 DDL。

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

### 4.1 压缩 HUGE 表定位遗留问题

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

当前结构观察：

- 主表对象保存逻辑列定义；
- 可能存在 `$AUX`、`$RAUX`、`$DAUX`、`$UAUX` 等内部辅助对象；
- 主表不呈现普通 BTREE storage；
- HUGE 存储入口可能不使用普通数据文件编号。

2026-07-04 全面测试结果：

| 表 | 类型 | 在线行数 | 离线结果 | 结论 |
| --- | --- | ---: | --- | --- |
| `SYSDBA.DMDUL_HUGE_COMP_T` | `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` | 5000 | `bootstrap` 解析到 `group=4,file=65535`，当前 `file.dict` 无对应文件，未生成可导出表字典 | 遗留问题，暂不支持 |

边界：

- 普通 `CREATE TABLE ... COMPRESS` 在当前测试库中没有让 `DBA_TABLES.COMPRESSION` 变为 `ENABLED`；
- `COMPRESS_MODE=1` 能设置参数值，但普通表仍显示 `COMPRESSION=DISABLED`；
- HUGE 表 `file=65535` 存储入口映射尚未解析；
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

无 `SYSTEM.DBF` 或 SYS 字典损坏时，不能执行标准表名导出；此时可用：

```sh
bic-dmdul bootstrap /recovery/dmcopy \
  --output-dir /recovery/work/scan_dict \
  --scan-storages-without-system-dicts \
  --sample-limit 8 \
  --json

bic-dmdul dump-data \
  --dict-dir /recovery/work/scan_dict \
  --output-dir /recovery/work/scan_out \
  --scan-storage-dict \
  --table SCAN.TAB_33596007 \
  --json
```

输出表只有一列 `raw_row VARBINARY`，内容是完整物理行 bytes。后续如果能提供字段列表，可再使用 orphan recovery 的字段适配导出。

### 5.1 TRUNCATE 后表数据残留与恢复特征

实验对象：

```text
表名:       SYSDBA.DMDUL_TRUNC_REC_T
表空间:     DMDUL_TS
行数:       22000
列:         ID INT, K INT, C2 CHAR(2), AMT DECIMAL(12,2), V VARCHAR(64), PAD VARCHAR(400)
object_id:  34177
storage_id: 33596006
group/file: 6/0
root page:  1712
数据文件:    /dmdata/data/DAMENG/DMDUL_TS01.DBF
```

TRUNCATE 前在线字典和离线字典表现：

| 项 | 值 |
| --- | --- |
| 在线行数 | `22000` |
| `DBA_SEGMENTS.HEADER_FILE` | `0` |
| `DBA_SEGMENTS.HEADER_BLOCK` | `1712` |
| `DBA_SEGMENTS.BYTES` | `8781824` |
| `tab.dict.storage_index_id` | `33596006` |
| `tab.dict.root_file/root_page` | `0/1712` |
| 原数据页范围 | 主要为 `1904..2967`，共 `1048` 个 `0x14` 数据页 |

TRUNCATE 后在线字典和离线字典表现：

| 项 | 值 |
| --- | --- |
| 在线行数 | `0` |
| 表对象 | `SYSOBJECTS` 中仍存在 |
| 列定义 | `SYSCOLUMNS` 中仍存在 |
| `DBA_SEGMENTS.HEADER_FILE` | 仍为 `0` |
| `DBA_SEGMENTS.HEADER_BLOCK` | 仍为 `1712` |
| `DBA_SEGMENTS.BYTES` | 缩小为 `131072` |
| `tab.dict.storage_index_id` | 仍为 `33596006` |
| `tab.dict.root_file/root_page` | 仍为 `0/1712` |
| 正常 `dump-data` | 从当前 root/segment 入口导出 `0` 行 |

关键 DBF 页特征：

- TRUNCATE 没有立即清空旧数据页内容。
- 旧数据页仍保持 `page_kind_raw=0x14`。
- 旧数据页页头 `storage_id_candidate` 仍为原 storage id：`33596006`。
- 旧页内行结构、slot、字段值仍可按原列定义正常解码。
- 实验中 TRUNCATE 后对旧页再次扫描：
  - `pages_checked=1048`
  - `pages_still_data_kind=1048`
  - `pages_same_storage_id=1048`
  - `good_rows=22000`
  - `min_id=1`
  - `max_id=22000`
- 样例页 `1904` 中仍能解码出 `ID=21, K=21, C2='TR', AMT=26.25, V='TRUNC_21'` 等完整字段。

恢复结论：

- 如果 TRUNCATE 后没有新的对象或 DML 覆盖这些旧页，数据具备恢复可能。
- 恢复不能依赖当前 segment root 的正常 leaf 链，因为当前字典入口已经代表“截断后”的空段状态。
- 可行策略是：先从历史记录、旧 `tab.dict`、操作前证据、备份字典、日志或人工记录中取得旧 storage id，再扫描同 group 的数据文件页头，找出仍带该 storage id 且 `page_kind_raw=0x14` 的数据页。普通表使用 `storage_index_id`；分区表使用所有 leaf 分区/subpartition 的 `storage_index_ids`。
- 找到候选页后仍必须按表列定义逐行解码，并跳过 deleted row；不能只按页头命中就无条件输出。
- 这种模式应作为显式恢复模式，不应放入默认导出路径。默认导出必须尊重当前字典入口，TRUNCATE 后当前表就是 0 行。

工具实现原则：

```text
正常导出:
  dict -> 当前 table storage root -> 当前 leaf/data page plan -> 导出当前活动行

TRUNCATE 恢复:
  dict/历史记录 -> 表列定义 + 旧 storage_index_id/storage_index_ids
  -> 全 group DBF 页头扫描
  -> page_kind_raw=0x14 且 storage id 命中 -> 解码活动行 -> 导出到恢复文件
```

边界和风险：

- 一旦旧页被重新分配并覆盖，无法从 DBF 中无损恢复被覆盖的数据。
- 如果只有 TRUNCATE 后的字典，而没有记录旧 storage id，仍可从当前 `tab.dict.storage_index_id` 尝试扫描；本次实验中 TRUNCATE 后 storage id 未变化。但这是否对所有达梦版本、所有表类型都稳定，还需要更多样本。
- 如果 TRUNCATE 后又插入新数据，新旧页可能同时带相同 storage id。恢复模式需要输出页号/行偏移，并提供后续去重或人工筛选依据。
- 分区表必须按 leaf partition/subpartition 的 storage id 分别扫描，不能只用父表对象。当前 `dump-data --truncate` 已自动读取 `tab.dict.storage_index_ids` 并逐个扫描。
- LOB 表需要额外处理：行内 locator 可能仍在旧行中，但 out-of-line LOB 页是否仍未覆盖需要单独按 locator 追踪验证，不能只恢复主表行。
- 该模式恢复的是“DBF 中仍残留的物理行”，不等价于数据库一致性读；事务可见性、未提交版本、回滚段关联仍是后续研究点。

本次实验证据文件保存在远端测试环境：

```text
/home/dmdba/dmdul/tmp/trunc_recovery/old_pages_manifest.json
/home/dmdba/dmdul/tmp/trunc_recovery/old_page_scan_after_truncate.json
/home/dmdba/dmdul/tmp/trunc_recovery/dict_before/
/home/dmdba/dmdul/tmp/trunc_recovery/dict_after/
```

### 5.2 DROP 后表数据残留与 orphan storage 发现

实验对象：

```text
表名:       SYSDBA.DMDUL_DROP_REC_T
表空间:     DMDUL_TS
行数:       18000
列:         ID INT, K INT, C2 CHAR(2), AMT DECIMAL(12,2), V VARCHAR(64), PAD VARCHAR(400)
object_id:  34178
storage_id: 33596007
group/file: 6/0
root page:  2944
```

DROP 前基线：

- 在线行数 `18000`；
- `DBA_SEGMENTS.HEADER_FILE=0`、`HEADER_BLOCK=2944`、`BYTES=7208960`；
- DROP 前 `tab.dict` 记录：
  - `storage_index_id=33596007`
  - `group_id=6`
  - `root_file=0`
  - `root_page=2944`
- checkpoint 后按 `storage_id=33596007` 扫描用户表空间，可导出 `18000` 行。

DROP 后在线字典表现：

| 查询 | 结果 |
| --- | ---: |
| `SYS.SYSOBJECTS WHERE NAME='DMDUL_DROP_REC_T'` | `0` |
| `DBA_TABLES WHERE OWNER='SYSDBA' AND TABLE_NAME='DMDUL_DROP_REC_T'` | `0` |
| `DBA_SEGMENTS WHERE OWNER='SYSDBA' AND SEGMENT_NAME='DMDUL_DROP_REC_T'` | `0` |

DROP 后离线观察：

- 常规 live slot 读取不到该表对象；
- SYSOBJECTS/SYSINDEXES/SYSCOLUMNS 的 slot 层统计中，本次样本没有出现 deleted slot：
  - `SYSOBJECTS deleted_slots=0`
  - `SYSINDEXES deleted_slots=0`
  - `SYSCOLUMNS deleted_slots=0`
- 但 `SYSTEM.DBF` 的 raw bytes 中仍残留：
  - 表名 `DMDUL_DROP_REC_T`，可识别 object id `34178`；
  - `SYSINDEXES` 残留行可识别 `storage_id=33596007`、`group_id=6`、`root_file=0`、`root_page=2944`；
  - `SYSCOLUMNS` raw scan 可识别 6 个列定义。
- 用户表空间中旧数据页仍存在：
  - `storage_id=33596007`
  - `pages_planned=858`
  - `rows_written=18000`
  - `decode_error=0`

因此 DROP 恢复至少有两种路径：

1. **字典 raw 残留可识别**
   - 从 `SYSTEM.DBF` raw bytes 找到表对象、列定义、SYSINDEXES storage root；
   - 构造临时 dict；
   - 再按 `storage_id` 扫描用户表空间导出。

2. **表名/对象 raw 残留也找不到**
   - 从当前 `tab.dict` 收集所有仍有归属的 `storage_index_id/storage_index_ids`；
   - 扫描用户表空间所有 `page_kind_raw=0x14` 数据页；
   - 将页头 storage id 不在当前字典集合中的对象列为 orphan storage；
   - 对每个 orphan storage 输出页数、首页列表、行 raw hex 和 ASCII hint，让用户确认；
   - 用户确认 storage id 后，用 `dump-data --orphan-scan-storage-id <id>` 导出。

本次 DROP 后执行 orphan storage 扫描：

```text
known_storage_ids=2536
orphan_candidates=1
storage_id=33596007
pages=858
first_pages=1904,1905,1906,1907,1908,1909,1910,1911
sample ascii:
  DROP_4725 ... DP ... Q13YYYY...
  DROP_4724 ... DP ... Q12YYYY...
```

结论：

- DROP 后旧表数据页没有立即清零，仍可恢复，前提是页未被重新分配覆盖；
- 本次样本不是 Oracle 式“字典行 slot 标 deleted 后仍在 slot 中”的形态，而是 live slot 已不可见、raw bytes 仍残留；
- 对 DROP 恢复，必须同时准备“raw 字典恢复”和“orphan storage 发现”两条路径；
- 如果没有列定义，只能先输出 raw row/ASCII hint 辅助识别；完整字段解码仍需要列定义或用户提供列定义。

本次实验证据文件保存在远端测试环境：

```text
/home/dmdba/dmdul/tmp/drop_recovery/dict_before/
/home/dmdba/dmdul/tmp/drop_recovery/dict_after/
/home/dmdba/dmdul/tmp/drop_recovery/dump_after_from_before_dict/dump.json
/home/dmdba/dmdul/tmp/drop_recovery/orphan_scan_after_drop.json
```

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
| `TIMESTAMP WITH LOCAL TIME ZONE` | 8 字节 timestamp；当前字典 scale 曾观察到 `4102`，这不是 SQL 小数秒精度 |
| `INTERVAL DAY TO SECOND` | 24 字节，前 5 个 int32 为 day/hour/min/sec/usec |
| `ROWID` | 12 字节，DM 显示为三段 4 字节 big-endian 的 base64 风格编码 |

DM8 测试库非 ASCII 字符当前按 GB18030 家族存储。解码策略是 ASCII fast path，其次 GB18030，再 UTF-8 fallback。

导入端 DDL 生成规则：

- `TIME/TIMESTAMP/DATETIME` 的合法 scale 为 `1..6` 时写为 `TYPE(scale)`。
- 带时区类型写为 DM 可接受语法，例如 `TIME(6) WITH TIME ZONE`、`DATETIME(6) WITH TIME ZONE`、`TIMESTAMP(6) WITH LOCAL TIME ZONE`。
- 如果字典 scale 超出 `1..6`，不把它写成 SQL 精度；例如 `TIMESTAMP WITH LOCAL TIME ZONE` 曾出现 `scale=4102`，DDL 应写为 `TIMESTAMP WITH LOCAL TIME ZONE`。
- 远端端到端验证发现，`TIME WITH TIME ZONE` 若建表时丢失 `(6)`，导入后小数秒会被四舍五入。当前已修复并通过 `DMDUL_TIME_TYPES` 双向 `MINUS=0/0` 验证。

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
bic-dmdul dump-data --dict-dir dulout --output-dir dumpout --table SYSDBA.T --output-format row
bic-dmdul import-data --input dumpout/SYSDBA.T.row --output-sql dumpout/SYSDBA.T.import.sql
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

2026-07-04 远端端到端导出、导入、比对结果：

| 表 | row 导出行数 | 导入目标 | 比对 |
| --- | ---: | --- | --- |
| `SYSDBA.DMDUL_MANY` | 80 | `DMTEST.RT_DMDUL_MANY` | 双向 `MINUS=0/0` |
| `SYSDBA.BMSQL_DISTRICT` | 100 | `DMTEST.RT_BMSQL_DISTRICT` | 双向 `MINUS=0/0` |
| `SYSDBA.BMSQL_WAREHOUSE` | 10 | `DMTEST.RT_BMSQL_WAREHOUSE` | 双向 `MINUS=0/0` |
| `SYSDBA.DMDUL_TIME_TYPES` | 2 | `DMTEST.RT_DMDUL_TIME_TYPES_FIX2` | 修复时间带时区精度后双向 `MINUS=0/0` |
| `SYSDBA.DMDUL_DUMP_TYPES` | 3 | `DMTEST.RT_DMDUL_DUMP_TYPES` | 非 LOB 标量列双向 `MINUS=0/0` |

## 11. 当前边界

- `SYSTEM.DBF` 缺失后的 `storage_scan.dict` 已支持 storage 聚合和 raw 行导出，但不能自动恢复真实表名/列定义；完整结构化恢复仍需要列定义或进一步字典 raw 残留解析。
- ASM 磁盘组读取尚未实现。
- 未提交事务、异常崩溃中间态、回滚未清理版本仍需单独实验。
- row tail/control 与完整 MVCC/undo 可见性尚未完全解析。
- 压缩 `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` 当前未通过全面测试：`SYSDBA.DMDUL_HUGE_COMP_T` 在线 5000 行，但离线 bootstrap 解析到 `group=4,file=65535` 且无法映射到普通 file.dict，未生成可导出表字典。HUGE/压缩表作为遗留问题，不应宣称已支持。
- 其他压缩形态、加密、特殊迁移行、跨文件 LOB 链尚未覆盖。
