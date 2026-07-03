# dmdul 中文使用手册

本文档说明 `dmdul` 当前阶段的完整使用方法。`dmdul` 是一个面向达梦 DM8 数据文件的离线数据导出工具，目标是在数据库实例不能正常启动、但数据文件仍可读取的情况下，从数据文件中恢复系统字典并导出用户表数据。

当前实现重点支持：

- 识别离线数据文件清单；
- 从 `SYSTEM.DBF` bootstrap 下载系统字典；
- 从字典定位表的 storage root；
- 解析表 storage BTree root/leaf page plan；
- 导出单表或某个用户下所有表的数据；
- 对大批量表导出支持多表并发 worker；
- 对大型分区表支持按分区名导出、表内 split-part 并发导出；
- 支持 DUL 文本、二进制 row 归档、parts manifest 三种导出结构；
- 支持从 DUL/row/parts 生成重装载 SQL；
- 支持已验证的短内联 LOB 和 out-of-line LOB 页链读取；
- 支持已验证的压缩 `HUGE TABLE` 主表导出，工具会自动映射到内部 `$RAUX` 行存储。

当前暂不作为主流程支持：

- 达梦 ASM 磁盘组读取；
- 缺失 `SYSTEM.DBF` 时的全文件扫描重组；
- 直接连接目标 DM 库并执行并发入库；
- 索引对象本身导出。

## 1. 基本原则

### 1.1 数据保真原则

导出工具的基本原则是：数据文件中看到什么，就尽量原样还原出来。

- 能准确理解的字段，按已验证的存储格式解码为可读值。
- 不能完整理解的字段，不得猜测、裁剪、转义、摘要化或改写。
- 不能完整理解但必须输出的 payload，应以 raw hex 形式保留。
- 页、行、列的解析必须以数据文件中的原始 bytes 和字典元数据为依据。
- 不允许为了 CSV 或 SQL 显示方便而丢失字节信息。

这条原则尤其适用于：`DATE/TIME/TIMESTAMP/DATETIME`、带时区时间类型、`CLOB/BLOB` locator、未知行尾控制区、未知 row metadata。

### 1.2 当前表数据定位原则

当前表数据导出不再依赖全面扫描整个数据文件。

正常路径是：

1. 从字典获取表的 `storage_id`、`root_file`、`root_page`。
2. 读取 root page。
3. 如果 root page 是 `0x14`，按 leaf/data 页处理。
4. 如果 root page 是 `0x15`，解析 BTree root/internal child pages。
5. 沿 leaf 页的 `next` 链导出数据页。
6. 每个计划页都校验 page identity、page kind、`storage_id`。
7. 如果 root/extent 解析还不能直接给出数据页，会按同 group 数据文件的页头 `storage_id` 做保守 fallback，找到 `0x14` 数据页后再导出。

这样可以处理生产环境中 extent 不连续、中间夹杂其他对象 extent 的情况。

## 2. 环境准备

### 2.1 工作目录

建议在项目根目录执行命令：

```sh
cd dmdul
mkdir -p tmp
```

本项目约定所有临时文件写入：

```text
./tmp
```

运行发布包命令时建议显式设置临时目录：

```sh
export TMPDIR=./tmp
```

使用以下形式运行：

```sh
TMPDIR=./tmp ./bin/dmdul <command> ...
```

### 2.2 数据文件准备

把需要恢复的达梦数据库文件复制到某个目录，例如：

```text
/recovery/dmcopy
```

目录中通常包括：

```text
SYSTEM.DBF
MAIN.DBF
ROLL.DBF
TEMP.DBF
业务表空间 DBF 文件
控制文件 dm.ctl / *.ctl，如果存在
```

当前阶段推荐至少保留 `SYSTEM.DBF`，因为 bootstrap 需要从系统字典中提取表、列、用户等信息。

### 2.3 文件复制注意事项

推荐使用数据库关闭后的冷备文件或存储快照。如果是数据库运行时拷贝，可能存在脏页、未 checkpoint、事务未完成等风险，导出结果需要额外核对。

## 3. 文件说明

### 3.1 init.dul

`init.dul` 是 dmdul 的默认参数文件。命令行参数可以覆盖其中部分配置。

当前支持的主要参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--filelist` | 数据文件清单路径 | `filelist.dul` |
| `--dirlist` | 数据文件目录列表，逗号分隔 | `.` |
| `--diskgroups` | ASM 磁盘组列表，当前暂不支持 | 空 |
| `--output_dir` | 用户表导出目录 | `dulout` |
| `--dict_dir` | 字典文件目录 | `dulout` |
| `--parallel` | 并发导出 worker 数 | `1` |
| `--page_size` | 数据页大小 | `8192` |
| `--data_delimiter` | 数据导出分隔符 | `|` |

示例：

```text
--filelist=/recovery/work/filelist.dul
--dirlist=/recovery/dmcopy
--diskgroups=
--output_dir=/recovery/work/dulout
--dict_dir=/recovery/work/dict
--parallel=4
--page_size=8192
--data_delimiter=|
```

说明：

- 参数行可以写成 `--key=value`。
- 空行、以 `#` 或 `;` 开头的行会被忽略。
- `data_delimiter` 当前建议使用 `|` 或 `~`，不要使用逗号。
- `dict_dir` 和 `output_dir` 可以相同，也可以分开。

### 3.2 filelist.dul

`filelist.dul` 记录数据文件清单，CSV 格式，每行：

```text
group_id,file_id,全路径文件名
```

示例：

```text
0,0,/recovery/dmcopy/SYSTEM.DBF
4,0,/recovery/dmcopy/MAIN.DBF
6,0,/recovery/dmcopy/DMDUL_TS01.DBF
```

含义：

- `group_id`：表空间/group id；
- `file_id`：该 group 内的数据文件编号；
- `path`：离线数据文件全路径。

`filelist.dul` 可以由 `prepare` 自动生成，也可以人工编辑。人工编辑后要确保路径存在且 group/file 编号正确。

### 3.3 字典文件

bootstrap 后会生成平面 CSV 字典文件，扩展名为 `.dict`：

| 文件 | 说明 |
| --- | --- |
| `file.dict` | 已识别数据文件清单 |
| `user.dict` | 用户/模式信息 |
| `tab.dict` | 表对象信息，当前只关心表，索引后续可补充 |
| `col.dict` | 表列定义 |
| `index.dict` 或相关索引字典 | 当前不是主流程必需 |

字典文件不用 JSON，是为了适应大型系统中几十万张表的场景，便于流式读取和内存缓存。

`tab.dict` 中和数据定位最相关的字段：

| 字段 | 含义 |
| --- | --- |
| `owner` / `name` / `qualified_name` | 表所属用户、表名、全名。判断同名表时必须同时看 owner。 |
| `object_id` | 表对象 id，用于和 `col.dict.object_id` 关联。 |
| `storage_index_id` | 普通表的 storage id；分区表父对象可能为空或只作为参考。 |
| `storage_index_ids` | 分区表所有 leaf 分区/subpartition 的 storage id 列表，使用 `;` 分隔。 |
| `group_id` / `root_file` / `root_page` | storage root 所在 group、文件号、页号。 |
| `page_refs` | 已展开的 leaf root 页引用，格式如 `0:1568;0:1584`。分区表导出优先使用它。 |
| `partition_names` | 与 `page_refs` 一一对应的 leaf 分区或子分区名，格式如 `P_LOW;P_HIGH`。 |
| `scan_pages` | 缺少更精确 page plan 时的保守扫描窗口。 |

`col.dict` 中和字段解码最相关的字段：

| 字段 | 含义 |
| --- | --- |
| `owner` / `table_name` / `qualified_table_name` | 列所属表。 |
| `object_id` | 与 `tab.dict.object_id` 关联。 |
| `ordinal` | 列顺序，导出时按该顺序解码。 |
| `name` | 列名。 |
| `type_name` | 达梦字段类型名。 |
| `length` / `scale` / `nullable` | 字段长度、精度、小数位、是否可空。 |

如果人工修改字典，必须保持 `tab.dict` 与 `col.dict` 的 `object_id` 对齐；分区表还要保持 `partition_names` 和 `page_refs` 数量一致、顺序一致。

## 4. 总体流程

推荐恢复流程分为三个阶段：

```text
prepare -> bootstrap -> dump-data
```

### 阶段 1：prepare

在 bootstrap 之前生成 `init.dul` 和 `filelist.dul`。优先使用 `dm.ctl` 取得数据库的数据文件清单；如果没有控制文件，再退回到目录扫描和 DBF 页头识别。

### 阶段 2：bootstrap

扫描 `SYSTEM.DBF`，下载系统字典，生成 `.dict` 文件。

### 阶段 3：dump-data

使用 bootstrap 字典定位表 storage root，解析 page plan，导出表数据。`--table` 和 `--user` 匹配不区分大小写。

## 5. 阶段 1：prepare

### 5.1 从 dm.ctl 生成 init.dul/filelist.dul

这是推荐方式，应放在 bootstrap 之前执行。适用于大多数直接在故障数据库服务器或数据库文件拷贝目录上提取的场景。

```sh
TMPDIR=./tmp ./bin/dmdul \
  prepare \
  --control-file /recovery/dmcopy/dm.ctl \
  --dirlist /recovery/dmcopy \
  --init-output /recovery/work/init.dul \
  --filelist-output /recovery/work/filelist.dul \
  --output-dir /recovery/work/dulout \
  --dict-dir /recovery/work/dict \
  --parallel 4 \
  --delimiter '|' \
  --json
```

`dm.ctl` 中记录的是原数据库的数据文件路径。发布包会用这些路径中的 DBF 文件名去 `--dirlist` 指定目录中寻找当前可读取的数据文件，然后从 DBF page0 页头读取 `group_id/file_id`，生成 `filelist.dul`。这样可以兼容“原路径不可用，但文件已复制到恢复目录”的场景。

也可以单独生成文件清单，便于检查：

```sh
TMPDIR=./tmp ./bin/dmdul \
  write-control-ctl \
  --control-file /recovery/dmcopy/dm.ctl \
  --dirlist /recovery/dmcopy \
  --output /recovery/work/filelist.dul \
  --json
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--control-file` | 指定 `dm.ctl` 或控制文件路径。推荐在 prepare 阶段显式指定。 |
| `--dirlist` | 存放 DBF 的目录清单，多个目录用逗号分隔。工具用控制文件里的 DBF 文件名到这些目录中匹配当前可读取文件。 |
| `--database-dir` | 离线数据库文件目录。不指定 `--control-file` 时可用该参数扫描整个目录。 |
| `--init-output` | 输出 `init.dul` 路径。默认当前目录 `init.dul`。 |
| `--filelist-output` | 输出 `filelist.dul` 路径。默认当前目录 `filelist.dul`。 |
| `--output-dir` | 后续表数据导出目录。 |
| `--dict-dir` | 后续字典文件目录。未指定时使用 `output_dir`。 |
| `--parallel` | 写入 init.dul 的并发度。 |
| `--delimiter` | 写入 init.dul 的数据分隔符。 |
| `--sample-limit` | 控制文件 DBF 路径扫描上限，默认 1000000，prepare 阶段按全量文件清单处理。 |
| `--json` | 输出 JSON 结果，便于脚本判断。 |

成功后会生成：

```text
/recovery/work/init.dul
/recovery/work/filelist.dul
```

### 5.2 从数据库目录生成 init.dul/filelist.dul

如果不显式指定 `--control-file`，也可以直接指定数据库目录。工具会扫描目录中的控制文件和 DBF，并生成 `filelist.dul`：

```sh
TMPDIR=./tmp ./bin/dmdul \
  prepare \
  --database-dir /recovery/dmcopy \
  --init-output /recovery/work/init.dul \
  --filelist-output /recovery/work/filelist.dul \
  --output-dir /recovery/work/dulout \
  --dict-dir /recovery/work/dict \
  --parallel 4 \
  --delimiter '|' \
  --json
```

### 5.3 没有控制文件时

如果没有控制文件，当前工具会根据目录中的数据文件页头进行识别：

```sh
TMPDIR=./tmp ./bin/dmdul \
  prepare \
  --dirlist /recovery/dmcopy,/recovery/more_dbs \
  --init-output /recovery/work/init.dul \
  --filelist-output /recovery/work/filelist.dul \
  --output-dir /recovery/work/dulout \
  --dict-dir /recovery/work/dict \
  --json
```

注意：如果页头损坏或文件不完整，自动识别结果可能需要人工校验 `filelist.dul`。

### 5.4 校验 filelist.dul

`prepare` 会返回 diagnostics。如果出现：

```text
filelist-empty
filelist-file-not-found
filelist-duplicate-group-file
```

需要先修正 `filelist.dul`。


## 6. 阶段 2：bootstrap 下载数据字典

### 6.1 使用 init.dul 执行 bootstrap

命令：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  bootstrap \
  -b \
  --json
```

`bootstrap` 是 `bootstrap-dicts` 的别名。

`-b` / `--download-dictionaries` 表示下载系统字典。

不使用 `--json` 时，bootstrap 会把执行进度输出到 stderr，例如：

```text
[bootstrap] start: database_dir=/recovery/dmcopy output_dir=/recovery/work/dict page_size=8192 download_dictionaries=True
[bootstrap] scan database directory: /recovery/dmcopy
[bootstrap] database scan complete: data_files=2 control_files=1
[bootstrap] write control.ctl: /recovery/work/dict/control.ctl
[bootstrap] download SYS dictionaries from SYSTEM storage roots
[bootstrap] dictionary rows: user=1 tab=10 col=80
[bootstrap] bootstrap complete
```

这些信息用于判断长时间执行时卡在目录扫描、SYS 字典存储根读取还是字典写入阶段。使用 `--json` 时 stdout 保持为纯 JSON，不输出这些进度行。

当前阶段 bootstrap 的核心任务：

- 找到 SYSTEM 表空间的数据文件；
- 从已知 SYS 字典存储根读取关键系统字典：
  - `SYSOBJECTS`；
  - `SYSINDEXES`；
  - `SYSCOLUMNS`；
- 生成 `file.dict`、`user.dict`、`tab.dict`、`col.dict`。

### 6.2 不使用 init.dul 的写法

也可以直接指定数据库目录和输出目录：

```sh
TMPDIR=./tmp ./bin/dmdul \
  bootstrap \
  /recovery/dmcopy \
  --output-dir /recovery/work/dict \
  -b \
  --scan-pages 64 \
  --json
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `database_dir` | 离线数据库文件目录，可省略并从 init.dul 的 `dirlist` 读取。 |
| `--output-dir` | 字典输出目录；如果使用 init.dul，可由 `dict_dir` 提供。 |
| `-b`, `--download-dictionaries` | 下载系统字典。实际恢复时应使用。 |
| `--owner` | 限定 owner，当前主要用于研究/目标表解析。 |
| `--table` | 指定目标表，当前主要用于研究/目标表解析，可重复。 |
| `--scan-pages` | 临时扫描窗口，默认 64。当前表导出已优先使用 BTree root page plan。 |
| `--catalog-pages` | bootstrap summary 时每个文件采样页数。 |
| `--sample-limit` | 采样数量。 |
| `--experimental-heuristic-dicts` | 研究用启发式字典输出，正常恢复不建议使用。 |
| `--json` | 输出 JSON。 |

### 6.3 bootstrap 输出文件

成功后目录中应包含：

```text
/recovery/work/dict/file.dict
/recovery/work/dict/user.dict
/recovery/work/dict/tab.dict
/recovery/work/dict/col.dict
```

可以快速查看：

```sh
head -5 /recovery/work/dict/tab.dict
head -5 /recovery/work/dict/col.dict
```

`tab.dict` 中当前只需要表对象；其他对象不是表数据导出主流程必需。

`file.dict` 是后续按表空间过滤扫描的重要依据。新版 bootstrap 会从控制文件 `dm.ctl` 解析数据文件到表空间的映射，并写入 `tablespace_name` 字段，例如：

```text
basename,group_id,file_no,tablespace_name
SYSTEM.DBF,0,0,SYSTEM
MAIN.DBF,4,0,MAIN
main2.dbf,4,1,MAIN
DMDUL_TS01.DBF,6,0,DMDUL_TS
```

注意：

- 表空间名来自控制文件，不从 DBF 文件名推断；
- 如果旧版本生成的 `file.dict` 没有 `tablespace_name`，`scan-orphan-storages --tablespace` 会拒绝执行；
- 遇到这种情况应使用新版工具重新执行 bootstrap，或者临时使用 `--group-id` 限定扫描范围。

## 7. 阶段 3：导出数据

### 7.1 导出单表

命令：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BMSQL_ORDERS \
  --json
```

`--table` 不区分大小写，下面两种写法等价：

```sh
./bin/dmdul --init-file /recovery/work/init.dul dump-data --table BMSQL.BMSQL_ORDERS
./bin/dmdul --init-file /recovery/work/init.dul dump-data --table bmsql.bmsql_orders
```

如果不使用 init.dul：

```sh
TMPDIR=./tmp ./bin/dmdul \
  dump-data \
  --dict-dir /recovery/work/dict \
  --output-dir /recovery/work/dulout \
  --table BMSQL.BMSQL_ORDERS \
  --delimiter '|' \
  --json
```

输出文件默认位于：

```text
/recovery/work/dulout/BMSQL.BMSQL_ORDERS.dul
```

文件格式：

```text
CREATE TABLE BMSQL.BMSQL_ORDERS (
  ...
);
-- DATA
列1|列2|列3
值1|值2|值3
```

说明：

- 文件头部包含简化建表 SQL；
- `-- DATA` 后是数据；
- 数据为分隔符文本，默认分隔符 `|`；
- 当前支持 `|` 和 `~`；
- 不建议使用逗号，因为业务数据中逗号出现概率较高。

### 7.2 二进制 row 归档格式

对于大表、包含换行/分隔符的字符列、二进制列或大 LOB 的表，推荐使用 row 归档格式：

```sh
TMPDIR=./tmp ./bin/dmdul \
  dump-data \
  --dict-dir /recovery/work/dict \
  --output-dir /recovery/work/dulout \
  --table BMSQL.BMSQL_ORDERS \
  --output-format row \
  --json
```

输出文件：

```text
/recovery/work/dulout/BMSQL.BMSQL_ORDERS.row
```

`.row` 是二进制文件，不是 JSON，也不是分隔文本。文件内包含：

- owner、表名、列定义；
- 可用于导入端建表的 `CREATE TABLE` 脚本；
- 每条活动行的原始 DM 行内 bytes；
- 行所在 file/page/slot offset；
- 该行引用的 LOB payload blocks。

row 归档可以复制到另一台服务器使用；导入端不需要原始 DBF，也不需要同名 `.lob/` 附件目录。

从 `.row` 生成导入 SQL：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql
```

默认会使用 `.row` 文件内的 `CREATE TABLE` 脚本，并生成 `INSERT` 和 `COMMIT`。如果目标库中表已经存在：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --no-create-table
```

如果需要导入到新表名：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --table RECOVER.BMSQL_ORDERS
```

`import-data` 同时支持 DUL 文本格式和 row 归档格式，默认 `--input-format auto` 会自动识别。已有 DUL 文本导出也可以生成导入 SQL：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.dul \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --input-format dul \
  --delimiter '|'
```

DUL 文本导入使用文件头部的 `CREATE TABLE`，并解析 `-- DATA` 后的分隔符数据；遇到 `@LOB:<relative-path>` 时，会从 DUL 文件所在目录读取同名 `.lob/` 附件。row 归档导入则使用 `.row` 文件内的建表脚本、原始行 bytes 和内嵌 LOB payload，不需要 `.lob/` 附件目录。`import-row` 仍保留为兼容别名，但新流程建议统一使用 `import-data`。

### 7.2.1 三种输出结构怎么选

| 输出结构 | 命令 | 主文件 | LOB 存放方式 | 适用场景 |
| --- | --- | --- | --- | --- |
| DUL 文本 | `--output-format dul` | `OWNER.TABLE.dul` | 默认写到 `OWNER.TABLE.lob/` 附件目录 | 小表、普通字符/数字表、需要人工检查文本内容 |
| row 归档 | `--output-format row` | `OWNER.TABLE.row` | LOB payload 内嵌到 `.row` 文件 | 大表、二进制列、包含分隔符/换行、大 LOB、跨服务器搬运 |
| parts manifest | `--partition-parallel > 1` | `OWNER.TABLE.dul` 或 `.row`，内容为 `DMDUL-PARTS 1` | 每个 part 独立处理；row part 内嵌 LOB，DUL part 使用自己的 `.lob/` | 大型分区表或 page refs 很多的表，需要表内并发导出 |

选择建议：

- 只为了快速查看少量数据，使用 DUL 文本。
- 恢复大表、LOB 表、含二进制列的表，优先使用 row 归档。
- 一个分区表有很多 leaf 分区或数据页很多，使用 `--partition-parallel` 生成 parts manifest。
- 要跨服务器传输，row 归档最省心，因为 LOB payload 在同一个归档体系中，不依赖原 DBF。
- 如果选择 DUL 文本并且有 LOB，复制到导入环境时必须连同 `.lob/` 目录一起复制。

### 7.2.2 DUL 文本文件结构

普通 DUL 文本主文件：

```text
CREATE TABLE OWNER.TABLE_NAME (
  C1 INT,
  C2 VARCHAR(100),
  C3 CLOB
);
-- DATA
C1|C2|C3
1|abc|@LOB:OWNER.TABLE_NAME.lob/00000001/C3.clob
```

对应 LOB 附件目录：

```text
OWNER.TABLE_NAME.lob/
  00000001/
    C3.clob
  manifest.jsonl
```

`manifest.jsonl` 每行描述一个 LOB 附件，字段包括：

| 字段 | 含义 |
| --- | --- |
| `table` | 表名 |
| `row_sequence` | 导出行序号，从 1 开始 |
| `column` | LOB 列名 |
| `type_name` | `TEXT` / `CLOB` / `BLOB` |
| `status` | `inline`、`out-of-line` 或 `unresolved-locator` |
| `file` | 附件相对路径 |
| `bytes` | 输出附件字节数 |
| `sha256` | 附件校验值 |
| `source_encoding` / `output_encoding` | 文本 LOB 的源编码和输出编码 |
| `source_bytes` | 原始 LOB payload 字节数 |
| `pages` | out-of-line LOB 页链页号 |

### 7.2.3 row 归档文件结构

`.row` 是二进制文件，不应使用文本编辑器修改。内部逻辑结构：

```text
MAGIC: DMDULROW
HEADER:
  owner
  table name
  create table SQL
  column metadata list
RECORDS:
  LOB record, optional, keyed by row sequence and column name
  ROW record, includes row sequence, file_no, page_no, row_offset, raw row bytes
END:
  rows count
```

row 归档保留的是 DM 原始行内 bytes；导入阶段再按归档中的列定义解码。这样可以避免 CSV 分隔符、换行、二进制字符造成歧义。

### 7.2.4 parts manifest 结构

启用 `--partition-parallel N` 后，主输出文件不是普通 DUL/row 数据，而是 manifest：

```text
DMDUL-PARTS 1
FORMAT row
TABLE OWNER.BIG_PART_T
DELIMITER |
PART_DIR OWNER.BIG_PART_T.row.parts
PART_COUNT 8
CREATE_SQL_BEGIN
CREATE TABLE OWNER.BIG_PART_T (
  ...
);
CREATE_SQL_END
PART 1 part-000001.row ROWS 10000 OK true
PART 2 part-000002.row ROWS 12000 OK true
```

part 子目录：

```text
OWNER.BIG_PART_T.row.parts/
  part-000001.row
  part-000002.row
  ...
```

导入时只需要把主 manifest 路径交给 `import-data`，工具会按 `PART_DIR` 找到所有 part 文件。

### 7.3 导出多个指定表

`--table` 可以重复：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BMSQL_ORDERS \
  --table BMSQL.BMSQL_CUSTOMER \
  --table BMSQL.BMSQL_STOCK \
  --json
```

### 7.4 导出某个用户下所有表

命令：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --user BMSQL \
  --json
```

### 7.5 导出指定分区

对于分区表，可以只导出一个 leaf 分区或几个 leaf 分区：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_PART_T \
  --partition P_LOW \
  --json
```

多个分区可以重复写 `--partition`，也可以使用逗号列表：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_PART_T \
  --partition P_LOW,P_HIGH \
  --partition DMHASHPART0 \
  --output-format row \
  --json
```

说明：

- 分区名大小写不敏感；
- `--partition` 依赖 bootstrap 写入的 `tab.dict.partition_names`，并与 `tab.dict.page_refs` 一一对应；
- 指定分区时本次必须只匹配一张表，避免把分区名误应用到多张表；
- 分区过滤可和 `--partition-parallel` 同时使用。

### 7.5.1 TRUNCATE 后残留数据恢复导出

如果表被 `TRUNCATE`，当前字典中的表对象和列定义通常仍存在，但当前段入口已经代表截断后的空表。此时默认 `dump-data` 会按当前入口导出，结果应为 0 行。

在旧数据页尚未被覆盖时，可以启用显式恢复模式：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table SYSDBA.DMDUL_TRUNC_REC_T \
  --truncate \
  --output-format row \
  --json
```

`--truncate` 会从 `tab.dict` 自动取得旧 storage id：

- 普通表读取 `storage_index_id`；
- 分区表读取 `storage_index_ids`，对所有 leaf 分区/subpartition 的 storage id 分别扫描；
- 工具扫描同 group 数据文件中 `page_kind_raw=0x14` 且页头 storage id 命中的旧数据页；
- 找到候选页后仍按 `col.dict` 的列定义逐行解码，并跳过 deleted row。

如果需要人工指定 storage id，也可以使用：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table SYSDBA.DMDUL_TRUNC_REC_T \
  --orphan-scan-storage-id 33596006 \
  --json
```

限制和注意事项：

- `--truncate` / `--orphan-scan-storage-id` 必须只匹配一张表；
- 当前不能和 `--partition`、`--partition-parallel` 组合；
- 分区表恢复会自动扫描全部 `storage_index_ids`，暂不支持只恢复其中几个分区；
- 恢复结果是 DBF 中残留的物理行，不等价于数据库一致性读；
- 如果旧页已经被重新分配并覆盖，无法从数据文件中恢复被覆盖的数据；
- TRUNCATE 后又插入新数据时，新旧页可能同时带相同 storage id，需要结合输出报告中的页号、行偏移进一步筛选。

### 7.5.2 DROP 后 orphan storage 扫描

如果表已经被 `DROP`，当前在线字典和 bootstrap live 字典可能都找不到完整表入口。此时可以先扫描当前字典没有归属的 storage id：

```sh
TMPDIR=./tmp ./bin/dmdul \
  scan-orphan-storages \
  --dict-dir /recovery/work/dict \
  --min-pages 4 \
  --sample-rows 3 \
  --json
```

默认不指定过滤条件时，会扫描 `file.dict` 中所有数据文件。如果大致知道表在哪个表空间，可以限制扫描范围：

```sh
TMPDIR=./tmp ./bin/dmdul \
  scan-orphan-storages \
  --dict-dir /recovery/work/dict \
  --tablespace DMDUL_TS \
  --min-pages 4 \
  --sample-rows 3 \
  --json
```

也可以直接使用 group/tablespace id：

```sh
TMPDIR=./tmp ./bin/dmdul \
  scan-orphan-storages \
  --dict-dir /recovery/work/dict \
  --group-id 6 \
  --min-pages 4 \
  --sample-rows 3 \
  --json
```

输出会列出候选 storage id：

```json
{
  "mode": "dm-scan-orphan-storages",
  "known_storage_ids": 2536,
  "candidates": [
    {
      "storage_id": 33596007,
      "group_id": 6,
      "file_no": 0,
      "pages": 858,
      "first_pages": [1904, 1905, 1906],
      "row_samples": [
        {
          "page_no": 1904,
          "offset": 7492,
          "len": 370,
          "raw_hex": "...",
          "ascii_hint": ".r..u...E...DP..<...DROP_4725.@Q13YYYY..."
        }
      ]
    }
  ]
}
```

确认候选 storage id 后，如果仍有旧字典、raw 字典恢复结果，或用户能提供列定义，可以用手工 storage id 导出：

```sh
TMPDIR=./tmp ./bin/dmdul \
  dump-data \
  --dict-dir /recovery/work/dict_or_recovered \
  --output-dir /recovery/work/drop_recover \
  --table SYSDBA.DMDUL_DROP_REC_T \
  --orphan-scan-storage-id 33596007 \
  --output-format row \
  --json
```

说明：

- `scan-orphan-storages` 不需要知道表名，只依赖当前 `tab.dict` 和 `file.dict`；
- 默认扫描全库；指定 `--tablespace` 或 `--group-id` 时只扫描对应表空间；
- `--tablespace` 可以重复，也可以逗号分隔；它必须依赖 `file.dict` 中由控制文件解析得到的 `tablespace_name`、`tablespace` 或 `group_name` 字段，不能从 DBF 文件名推断；
- 如果当前 `file.dict` 还没有表空间名映射，请使用 `--group-id`，或先用新版工具重新 bootstrap，让工具从 `dm.ctl` 补充数据文件到表空间的映射；
- 它把当前字典中没有归属、但用户表空间仍有 `0x14` 数据页的 storage id 列出来；
- `row_samples` 只用于人工识别，未必能完整解码字段；
- 完整字段导出仍需要列定义。列定义可能来自 DROP 前 dict、`SYSTEM.DBF` raw 残留、备份元数据或人工提供；
- 如果旧页已被重用覆盖，该 storage id 的页数、样例内容和行解码结果都会变差，需要人工判断。

### 7.6 导出压缩 HUGE 表

达梦压缩 `HUGE TABLE` 不是普通 BTREE 表段。已验证的建表语法示例：

```sql
CREATE HUGE TABLE SYSDBA.DMDUL_HUGE_COMP_T (
  ID INT,
  K INT,
  C2 CHAR(2),
  V VARCHAR(64),
  PAD VARCHAR(1000)
) COMPRESS LEVEL 1 FOR 'QUERY LOW';
```

在线字典中该表表现为：

- `DBA_TABLES.COMPRESSION = ENABLED`；
- 主表在 `DBA_SEGMENTS` 中可能显示 `HEADER_FILE=-1`、`HEADER_BLOCK=-1`、`BYTES=0`；
- `SYSOBJECTS` 中会出现同名内部辅助表，如：
  - `DMDUL_HUGE_COMP_T$AUX`
  - `DMDUL_HUGE_COMP_T$RAUX`
  - `DMDUL_HUGE_COMP_T$DAUX`
  - `DMDUL_HUGE_COMP_T$UAUX`

当前验证表明，主表的逻辑行数据存放在 `主表名$RAUX` 中，`$RAUX` 的列结构与主表一致。`bootstrap` 下载字典时会看到主表和这些辅助表；`dmdul` 在装配离线元数据时，如果发现主 HUGE 表没有普通 storage、但存在同 owner 的 `$RAUX` storage，会自动把主表列定义与 `$RAUX` storage 组合成可导出的主表元数据。

因此用户仍然使用主表名导出，不需要手工指定 `$RAUX`：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table SYSDBA.DMDUL_HUGE_COMP_T \
  --output-format row \
  --json
```

已验证用例：

| 表 | 类型 | 行数 | 导出结果 | 导入比对 |
| --- | --- | ---: | --- | --- |
| `SYSDBA.DMDUL_HUGE_COMP_T` | `HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'` | 5000 | `rows_written=5000`, `decode_error=0` | 导入 `DMTEST.DMDUL_HUGE_COMP_T_RT` 后双向 `MINUS=0` |

注意：

- 普通 `CREATE TABLE ... COMPRESS` 在当前测试库中并未让 `DBA_TABLES.COMPRESSION` 变为 `ENABLED`，不能作为压缩表测试依据。
- `COMPRESS_MODE=1` 是建表缺省压缩参数，但当前测试中普通表仍显示 `COMPRESSION=DISABLED`。
- 当前支持结论只覆盖已验证的压缩 `HUGE TABLE` + `$RAUX` 行存储形态。其他压缩等级、列级压缩、`QUERY HIGH`、带分区/LOB 的 HUGE 压缩表还需要独立测试。

### 7.7 并发导出

当用户下表很多时，可以启用多表并发：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --user BMSQL \
  --parallel 8 \
  --json
```

也可以写入 `init.dul`：

```text
--parallel=8
```

说明：

- `parallel=1` 为单表导出队列；
- `parallel>1` 会使用多个 worker 同时导出多张表；
- 并发度不宜超过磁盘实际吞吐能力；
- 大量小表可以适当提高并发；
- 大表导出主要受单表扫描和磁盘顺序读取影响。

对于大型分区表，可以启用表内 split-part 并发：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_PART_T \
  --partition-parallel 8 \
  --output-format row \
  --json
```

此时主输出文件仍是：

```text
/recovery/work/dulout/BMSQL.BIG_PART_T.row
```

但它是一个 parts manifest，内容记录 part 子目录位置、建表脚本和每个 part 文件：

```text
DMDUL-PARTS 1
FORMAT row
TABLE BMSQL.BIG_PART_T
PART_DIR BMSQL.BIG_PART_T.row.parts
CREATE_SQL_BEGIN
...
CREATE_SQL_END
PART 1 part-000001.row ROWS ...
PART 2 part-000002.row ROWS ...
```

每个 worker 只写自己的 part 文件，例如：

```text
BMSQL.BIG_PART_T.row.parts/
  part-000001.row
  part-000002.row
  part-000003.row
```

`import-data` 可以直接读取主 manifest，并自动找到 part 子目录：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BIG_PART_T.row \
  --output-sql /recovery/work/dulout/BMSQL.BIG_PART_T.import.sql
```

当前导入命令会把所有 part 合并生成一个 SQL 文件；文件结构已经保留了 part 边界，后续直接执行导入时可以按 part 文件启动多个导入 worker。

### 7.8 导出报告

可以把 JSON 报告写入文件：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --user BMSQL \
  --parallel 8 \
  --report-output /recovery/work/dulout/report.json \
  --json
```

报告中包含：

```text
tables_total
tables_ok
tables_failed
每张表 rows_written
每张表 decode_errors
每张表 diagnostics
每张表 scanned_pages / scanned_page_refs
```

典型 JSON 结构：

```json
{
  "mode": "dm-dump-data",
  "dict_dir": "/recovery/work/dict",
  "output_dir": "/recovery/work/dulout",
  "delimiter": "|",
  "output_format": "row",
  "parallel": 8,
  "partition_parallel": 1,
  "tables_total": 10,
  "tables_ok": 10,
  "tables_failed": 0,
  "reports": [
    {
      "table": "BMSQL.BMSQL_ORDERS",
      "output": "/recovery/work/dulout/BMSQL.BMSQL_ORDERS.row",
      "ok": true,
      "strict_ok": true,
      "rows_written": 100000,
      "rows_skipped_deleted": 0,
      "rows_skipped_decode_error": 0,
      "decode_errors": [],
      "diagnostics": [],
      "scanned_page_refs": [
        {"file_no": 0, "page_no": 96}
      ],
      "mode": "page-plan-btree-root-children"
    }
  ]
}
```

字段解释：

| 字段 | 含义 |
| --- | --- |
| `ok` | 没有 error 级 diagnostics。 |
| `strict_ok` | 没有 error，也没有严格模式认为有风险的 warning。 |
| `rows_written` | 成功写出的活动行数。 |
| `rows_skipped_deleted` | 物理上存在但标记删除的行数。 |
| `rows_skipped_decode_error` | 因行结构/类型解码失败跳过的行数。 |
| `decode_errors` | 最多保留前 10 个解码错误位置。 |
| `diagnostics` | page plan、LOB、类型、文件缺失等诊断信息。 |
| `scanned_page_refs` | 实际接受并扫描的数据页 file/page。 |
| `mode` | 本表 page plan 或导出模式。 |

不使用 `--json` 时，如果有失败表，工具会把失败详情输出到 stderr，例如：

```text
table_failed=TEST2.BMSQL_ITEM
  output=/recovery/work/dulout/TEST2.BMSQL_ITEM.dul
  rows_written=0
  diagnostic=dump-data-table-failed level=error message=[Errno 2] No such file or directory: '/recovery/dmcopy/DMDUL_TS01.DBF'
```

如果看到 `tables_failed>0`，优先查看 stderr 中的 `table_failed`、`diagnostic` 和 `decode_error`。也可以同时加 `--report-output` 保存完整 JSON 报告。

### 7.9 strict page plan

默认情况下，如果 page plan 有可恢复的 warning，工具会继续导出并在 report 中记录 diagnostics。

如果希望 page plan 不完整时直接失败：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BMSQL_ORDERS \
  --strict-page-plan \
  --json
```

生产恢复建议先不使用 `--strict-page-plan` 完成最大化导出，再根据 report 复核 diagnostics。

### 7.10 导入和重装载 SQL

`import-data` 的作用是把 dmdul 导出的文件转换成可在目标 DM 数据库执行的 SQL。当前它不直接连接数据库执行 SQL，而是生成 `.sql` 文件，便于审计、分批执行或后续接入并发执行器。

基本命令：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --json
```

支持输入格式：

| `--input-format` | 含义 |
| --- | --- |
| `auto` | 默认。自动识别 row magic、parts manifest，其他按 DUL 文本处理。 |
| `dul` | 强制按 DUL 文本解析。 |
| `row` | 强制按二进制 row 归档解析。 |
| `parts` | 强制按 parts manifest 解析。 |

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--input` | DUL、row 或 parts manifest 输入文件。 |
| `--output-sql` | 输出 SQL 文件。 |
| `--input-format` | 输入格式，默认 `auto`。 |
| `--delimiter` | DUL 文本分隔符；不指定时从数据表头自动判断。 |
| `--table` | 指定目标表名。用于导入到不同 schema 或临时恢复表。 |
| `--no-create-table` | 不输出建表脚本，只输出 `INSERT` 和 `COMMIT`。 |
| `--json` | 输出导入报告。 |

默认 SQL 结构：

```sql
CREATE TABLE OWNER.TABLE_NAME (
  ...
);

INSERT INTO OWNER.TABLE_NAME (C1, C2) VALUES (...);
INSERT INTO OWNER.TABLE_NAME (C1, C2) VALUES (...);
COMMIT;
```

如果目标表已提前建好：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.insert.sql \
  --no-create-table
```

如果要导入到新表名：

```sh
TMPDIR=./tmp ./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS_REC.import.sql \
  --table RECOVER.BMSQL_ORDERS_REC
```

LOB 导入规则：

- DUL 文本遇到 `@LOB:<relative-path>` 时，从输入 DUL 文件所在目录读取附件；
- row 归档从 `.row` 文件内部读取 LOB payload；
- parts manifest 会逐个读取 part 文件，每个 part 按自己的格式解析；
- `BLOB` 输出为 `HEXTORAW('<hex>')`；
- `CLOB/TEXT` 输出为字符串字面量，当前按 UTF-8 文本写入 SQL。

注意：对于超大 LOB，单条 SQL 字面量可能受目标数据库 SQL 长度限制。当前工具先保证导出结构完整和可审计；后续直接入库执行器应使用分块绑定或批量装载方式处理超大 LOB。

### 7.11 dump-data 参数组合规则

`dump-data` 当前是正式导出主入口。常用参数：

| 参数 | 说明 |
| --- | --- |
| `--dict-dir` | bootstrap 输出的字典目录。不使用 init.dul 时必须指定。 |
| `--output-dir` | 表数据输出目录。不使用 init.dul 时必须指定。 |
| `--table` | 指定表，可重复。支持 `OWNER.TABLE` 或只写表名；只写表名时可能匹配多个 owner 的同名表，应谨慎。 |
| `--user` | 导出某个用户下所有表。按 owner 匹配，和表名无关。 |
| `--partition` | 指定 leaf 分区/子分区名。可重复，也可逗号分隔。要求本次只匹配一张表。 |
| `--parallel` | 多表并发 worker 数。 |
| `--partition-parallel` | 单表内部 split-part worker 数。大分区表使用。 |
| `--truncate` | TRUNCATE 恢复模式。自动从 `tab.dict.storage_index_id/storage_index_ids` 取旧 storage id 并扫描残留数据页。 |
| `--orphan-scan-storage-id` | 手工指定旧 storage id 扫描残留数据页，适合 TRUNCATE/DROP 恢复实验。 |
| `--delimiter` | DUL 文本分隔符，支持 `|` 和 `~`。 |
| `--output-format` | `dul` 或 `row`。默认 `dul`。 |
| `--report-output` | 写 JSON 报告到文件。 |
| `--strict-page-plan` | page plan 不完整时直接失败。 |
| `--lob-mode` | `external` 或 `inline`。默认 `external`。row 归档总是按可导入方式保存 LOB payload。 |
| `--lob-hash` | LOB 附件 hash，目前支持 `sha256`。 |
| `--json` | stdout 输出 JSON manifest。 |

选择对象时的规则：

- 不指定 `--table` 且不指定 `--user`：导出字典中的全部表。
- 指定一个或多个 `--table`：只导出匹配表。
- 指定 `--user`：导出该 owner 下全部表。
- 同时指定 `--table` 和 `--user`：当前过滤条件是并集，通常不建议这样用；需要精确导出时优先使用 `--table OWNER.TABLE`。
- 指定 `--partition`：必须最终只匹配一张表，否则命令返回错误。

并发参数的区别：

| 参数 | 并发对象 | 输出结构 |
| --- | --- | --- |
| `--parallel` | 多张表 | 每张表一个主输出文件 |
| `--partition-parallel` | 同一张表内的多个 page refs/分区切片 | 一个主 manifest + parts 子目录 |

常见组合：

```sh
# 单表 row 归档
dmdul dump-data --dict-dir dict --output-dir out \
  --table BMSQL.T1 --output-format row

# 用户全量，多表并发
dmdul dump-data --dict-dir dict --output-dir out \
  --user BMSQL --parallel 8

# 大分区表，表内并发，每个 worker 写 part
dmdul dump-data --dict-dir dict --output-dir out \
  --table BMSQL.BIG_PART_T --partition-parallel 8 --output-format row

# 只导出几个分区，并对这些分区 split-part
dmdul dump-data --dict-dir dict --output-dir out \
  --table BMSQL.BIG_PART_T --partition P_LOW,P_HIGH \
  --partition-parallel 2 --output-format row
```

## 8. 当前 page plan 机制

当前 `dump-data` 的 page plan 顺序：

1. 如果字典或 manifest 中已有显式 page refs，直接使用。
2. 如果已有 page numbers，沿同文件 leaf 链遍历。
3. 如果有 `storage_id`，读取 root page。
4. root kind `0x14`：按 root leaf page 导出。
5. root kind `0x15`：解析 BTree root/internal child pages，再沿 leaf `next` 链导出。
6. root 无法解析时，退回 storage-id scan window，并写 diagnostics。
7. 没有 storage_id 时，才使用 root 起始的 scan range fallback。

当前对大表 `DMDUL_MANY` 的证据：

```text
root page = 80
root kind = 0x15
leftmost child = 96
root entry child pages = 97..135
planned pages = 96..135
rows_written = 80
```

这说明当前实现不依赖全面扫描文件，也不要求 extent 连续。

## 9. 常见输出 diagnostics

| code | 含义 | 处理建议 |
| --- | --- | --- |
| `page-plan-root-leaf-chain` | 从 `0x14` root leaf 链生成 page plan | 正常信息 |
| `page-plan-btree-root-children` | 从 `0x15` BTree root/internal 页生成 page plan | 正常信息 |
| `page-plan-storage-id-scan` | root 结构无法直接解析，退回 root 附近 storage_id scan | 需要复核，但可能仍可导出 |
| `page-plan-storage-id-global-scan` | root 附近没有数据页时，按同 group DBF 页头 storage_id 查找 `0x14` 数据页 | 保守 fallback，正确性优先但大文件较慢 |
| `page-plan-fallback-scan-range` | 没有 storage_id/page refs，只能按 root 起扫窗口 | 风险较高，应补字典/root 信息 |
| `page-plan-file-missing` | filelist 中缺少数据文件 | 修正 `filelist.dul` |
| `page-plan-out-of-range` | 计划页超过文件大小 | 检查文件是否完整或字典是否错配 |
| `page-plan-identity-mismatch` | 页头 file/page 与预期不符 | 检查 filelist 或数据文件版本 |
| `page-scan-skipped-storage-id-mismatch` | 页 storage_id 不匹配，被跳过 | 可能是 scan fallback 扫到其他对象页 |
| `unsupported-column-type` | 某列类型当前不能解码 | 需要补类型解码或 raw 输出策略 |
| `row-decode-error` | 行解码失败 | 查看 report 中 decode_errors |
| `unsupported-row-metadata` | 行 metadata/NULL bitmap 等尚未完全支持 | 需要进一步分析行结构 |

## 10. 数据类型支持状态

当前已实现或阶段性支持：

| 类型 | 状态 |
| --- | --- |
| `TINYINT` | 支持 |
| `SMALLINT` | 支持 |
| `INT` / `INTEGER` | 支持 |
| `BIGINT` | 支持 |
| `REAL` | 支持，4 字节 IEEE float |
| `FLOAT` | 支持，按字典长度决定 4/8 字节，默认 8 字节 |
| `DOUBLE` | 支持，8 字节 IEEE double |
| `NUMBER` / `NUMERIC` / `DECIMAL` | 支持 base-100 变长解码 |
| `DATE` | 支持当前观测到的页内 payload；未知额外字段必须保留 |
| `TIME` | 支持当前观测到的 5 字节 payload |
| `TIMESTAMP` / `DATETIME` | 支持当前观测到的 8 字节 payload |
| `CHAR` / `VARCHAR` / `VARCHAR2` | 支持 |
| `TEXT` / `CLOB` / `BLOB` | 支持短内联 LOB；支持当前已验证的 21 字节 out-of-line locator 和 `0x20` LOB 页链；默认外置到 `.lob/` 附件目录 |
| 带时区时间类型 | 支持当前观测到的 TZ offset 编码 |

注意：如果某个类型包含未理解的 extra bytes，应优先 raw hex 保留，不得丢失。

## 11. 调试和研究命令

以下命令主要用于排查数据文件和结构研究，普通导出流程不一定需要。

### 11.1 查看文件信息

```sh
TMPDIR=./tmp ./bin/dmdul \
  file-info /recovery/dmcopy/SYSTEM.DBF
```

输出文件大小、页大小、页数。

### 11.2 dump 单个 page

```sh
TMPDIR=./tmp ./bin/dmdul \
  dump-page /recovery/dmcopy/DMDUL_TS01.DBF 80 --bytes 256
```

按十六进制输出 page 内容。

### 11.3 inspect page header

```sh
TMPDIR=./tmp ./bin/dmdul \
  inspect-page /recovery/dmcopy/DMDUL_TS01.DBF 80 --rows --dump 128
```

输出当前已识别的 page header 字段、行偏移、删除标志等。

### 11.4 查找字符串 marker

```sh
TMPDIR=./tmp ./bin/dmdul \
  find /recovery/dmcopy/SYSTEM.DBF DMDUL_MANY
```

用于在数据文件中查找对象名、测试 marker 等。

### 11.5 catalog pages

```sh
TMPDIR=./tmp ./bin/dmdul \
  catalog-pages /recovery/dmcopy/DMDUL_TS01.DBF \
  --start-page 0 \
  --max-pages 512 \
  --output /recovery/work/page_catalog.json
```

用于扫描文件页头，统计 page kind、storage_id 等。

### 11.6 analyze-block

```sh
TMPDIR=./tmp ./bin/dmdul \
  analyze-block /recovery/dmcopy/DMDUL_TS01.DBF 224 \
  --column ID:INT:4 \
  --column N38:NUMBER:22 \
  --column D:DATE:3 \
  --column TS:TIMESTAMP:8 \
  --column MARKER:VARCHAR:64
```

用于分析单个数据页中的行和列解码。

也可以从 `col.dict` 读取列定义：

```sh
TMPDIR=./tmp ./bin/dmdul \
  analyze-block /recovery/dmcopy/DMDUL_TS01.DBF 224 \
  --columns-jsonl /recovery/work/dict/col.dict
```

### 11.7 dump unknown structures

```sh
TMPDIR=./tmp ./bin/dmdul \
  dump-unknown-structures /recovery/dmcopy/DMDUL_TS01.DBF \
  --pages 80,81,96-98 \
  --output /recovery/work/unknown_pages.json
```

用于探索 segment root、metadata page、row tail 等未知结构。

### 11.8 summarize / preflight

```sh
TMPDIR=./tmp ./bin/dmdul \
  summarize-database /recovery/dmcopy \
  --output /recovery/work/summary.json
```

```sh
TMPDIR=./tmp ./bin/dmdul \
  preflight-database /recovery/dmcopy \
  --output /recovery/work/preflight.json
```

用于恢复前检查数据文件集合。

## 12. 旧入口和兼容命令

### 12.1 extract-dicts

`extract-dicts` 是早期基于 dict 目录导出表的入口。当前主流程推荐使用 `dump-data`。

示例：

```sh
TMPDIR=./tmp ./bin/dmdul \
  extract-dicts \
  --dict-dir /recovery/work/dict \
  --output-dir /recovery/work/dulout \
  --table BMSQL.BMSQL_ORDERS \
  --workers 4 \
  --json
```

### 12.2 extract-csv

`extract-csv` 是研究和调试入口，可以从 JSON metadata、segment manifest 或 database dir 直接导出一张表。

示例：

```sh
TMPDIR=./tmp ./bin/dmdul \
  extract-csv \
  --metadata-json /recovery/work/table_metadata.json \
  --table BMSQL.BMSQL_ORDERS \
  --output /recovery/work/BMSQL_ORDERS.csv
```

正常恢复流程不建议优先使用这个入口。

## 13. 推荐生产操作步骤

以下是一套完整命令模板。

### 13.1 设置环境变量

```sh
cd dmdul
mkdir -p tmp /recovery/work/dict /recovery/work/dulout
export TMPDIR=./tmp
```

### 13.2 生成 init.dul 和 filelist.dul

```sh
./bin/dmdul \
  prepare \
  --control-file /recovery/dmcopy/dm.ctl \
  --dirlist /recovery/dmcopy \
  --init-output /recovery/work/init.dul \
  --filelist-output /recovery/work/filelist.dul \
  --output-dir /recovery/work/dulout \
  --dict-dir /recovery/work/dict \
  --parallel 4 \
  --delimiter '|' \
  --json
```

### 13.3 检查 init.dul

```sh
cat /recovery/work/init.dul
cat /recovery/work/filelist.dul
```

确认：

- `filelist` 指向正确路径；
- `dict_dir` 指向字典目录；
- `output_dir` 指向导出目录；
- `filelist.dul` 中的文件都存在；
- `SYSTEM.DBF` 所在 group/file 正确。

### 13.4 下载字典

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  bootstrap \
  -b \
  --json
```

确认字典文件：

```sh
ls -l /recovery/work/dict
head -5 /recovery/work/dict/user.dict
head -5 /recovery/work/dict/tab.dict
head -5 /recovery/work/dict/col.dict
```

### 13.5 先导出一张表试跑

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BMSQL_ORDERS \
  --report-output /recovery/work/dulout/orders_report.json \
  --json
```

检查：

```sh
head -50 /recovery/work/dulout/BMSQL.BMSQL_ORDERS.dul
cat /recovery/work/dulout/orders_report.json
```

重点看：

- `ok` 是否为 true；
- `rows_written` 是否符合预期；
- 是否有 `row-decode-error`；
- page plan 是否为 `page-plan-root-leaf-chain` 或 `page-plan-btree-root-children`。

### 13.6 导出用户所有表

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --user BMSQL \
  --parallel 8 \
  --report-output /recovery/work/dulout/bmsql_report.json \
  --json
```

### 13.7 大表推荐使用 row 归档

对于包含大字段、二进制字段、LOB、换行或分隔符的表，建议直接导出 row 归档：

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_TABLE \
  --output-format row \
  --report-output /recovery/work/dulout/big_table_report.json \
  --json
```

输出：

```text
/recovery/work/dulout/BMSQL.BIG_TABLE.row
```

如果要在另一台服务器导入，只需要复制 `.row` 文件；如果输出是 DUL 文本并且有 LOB，则必须同时复制 `.dul` 和 `.lob/` 目录。

### 13.8 大型分区表 split-part 导出

对于 leaf 分区很多或单表数据很大的分区表：

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_PART_T \
  --partition-parallel 8 \
  --output-format row \
  --report-output /recovery/work/dulout/big_part_report.json \
  --json
```

输出：

```text
/recovery/work/dulout/BMSQL.BIG_PART_T.row
/recovery/work/dulout/BMSQL.BIG_PART_T.row.parts/
```

主 `.row` 文件此时是 parts manifest，不是单个 row archive。复制到导入环境时必须同时复制 manifest 和 `.parts/` 子目录。

只导出指定分区：

```sh
./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --table BMSQL.BIG_PART_T \
  --partition P202401,P202402 \
  --partition-parallel 2 \
  --output-format row \
  --json
```

### 13.9 生成导入 SQL

普通 row 归档：

```sh
./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BIG_TABLE.row \
  --output-sql /recovery/work/dulout/BMSQL.BIG_TABLE.import.sql \
  --json
```

parts manifest：

```sh
./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BIG_PART_T.row \
  --output-sql /recovery/work/dulout/BMSQL.BIG_PART_T.import.sql \
  --json
```

DUL 文本：

```sh
./bin/dmdul \
  import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.dul \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --delimiter '|' \
  --json
```

### 13.10 复核失败表

如果 report 中 `tables_failed > 0`，先查看失败表 diagnostics：

```sh
cat /recovery/work/dulout/bmsql_report.json
```

常见处理：

- 缺文件：修正 `filelist.dul`；
- 类型不支持：保留 evidence，补类型解码或 raw hex 输出；
- row metadata 不支持：用 `analyze-block` 和 `dump-unknown-structures` 分析；
- page plan fallback：检查 `tab.dict` 中 storage_id/root_page 是否正确。

## 14. 输出文件命名

单表导出文件名通常为：

```text
OWNER.TABLE.dul
```

例如：

```text
BMSQL.BMSQL_ORDERS.dul
TEST2.DMDUL_MANY.dul
```

文件名中不适合文件系统的字符会被替换为 `_`。

## 15. 当前限制和风险

### 15.1 SYSTEM.DBF 缺失

当前主流程依赖 `SYSTEM.DBF` 下载系统字典。如果 `SYSTEM.DBF` 丢失，后续需要实现全文件扫描、按 `storage_id` 聚合对象、重组表结构的模式。这个功能当前不是主流程。

### 15.2 LOB

`TEXT/CLOB/BLOB` 默认外置导出。主 DUL/CSV 文件中写入 `@LOB:<relative-path>` 占位符，实际内容写入同名 `.lob/` 目录，并生成 `manifest.jsonl`。

当前已验证两类 LOB：

- 短内联 LOB：行内 13 字节前缀后跟真实 payload。
- out-of-line LOB：行内 21 字节 locator 指向 `0x20` LOB 数据页，工具按 `next_page` 链读取完整 payload。

`BLOB` 附件按原始 bytes 写出；`CLOB/TEXT` 附件解码后写 UTF-8，manifest 中记录 `source_encoding`、`source_bytes`、输出 `bytes` 和 `sha256`。如果遇到不符合当前已验证格式的 locator，工具不会伪造成功，会写 `.locator.hex` 并报告 `lob-locator-not-followed`。

使用 `--output-format row` 时，LOB payload 直接写入 `.row` 文件内的 LOB block，不再依赖外置 `.lob/` 附件目录。导入端只需要 `.row` 文件即可生成包含 LOB 值的 SQL。

### 15.3 压缩表

当前已验证支持一种达梦压缩表形态：`HUGE TABLE ... COMPRESS LEVEL 1 FOR 'QUERY LOW'`。这种表的主对象没有普通 BTREE storage，逻辑行位于内部 `主表名$RAUX` 表中。`dmdul` 会在离线字典装配阶段自动把主表映射到 `$RAUX` storage，用户仍按主表名导出。

尚未覆盖的压缩相关场景：

- `QUERY HIGH`；
- 列级压缩或 `EXCEPT` 列排除；
- 压缩 HUGE 分区表；
- 压缩 HUGE 表包含 LOB；
- 其他版本/参数组合下的普通表压缩。

如果遇到未验证压缩形态导致 `row-decode-error`、`unsupported-row-metadata` 或找不到 storage，应保留 raw evidence，不得猜测导出。

### 15.4 未知行结构

遇到未知 row metadata、NULL bitmap 变体、迁移行、行外列等情况时，当前可能产生 `row-decode-error` 或 `unsupported-row-metadata`。此时应保留 raw evidence，不得猜测导出。

### 15.5 索引

表数据导出当前不依赖独立索引段分析。表 storage 对象本身可能是 BTree 组织；独立主键/普通索引需要后续通过 `SYSINDEXES` 区分。目前不建议把表 storage BTree 和独立索引段混为一谈。

### 15.6 ASM

`init.dul` 中预留了 `diskgroups` 参数，但当前阶段暂不支持 ASM 磁盘组读取。

### 15.7 parts 并发导入边界

`--partition-parallel` 已经支持导出端多 worker、每个 worker 写独立 part 文件。`import-data` 当前可以读取 parts manifest，并把所有 part 合并生成一个 SQL 文件。

尚未实现的是：直接连接目标 DM 数据库，按 part 启动多个导入 worker 并行执行。后续实现时应优先使用绑定变量、批量提交或数据库装载接口，而不是为超大 LOB 生成巨大的单条 SQL 字面量。

### 15.8 超大 LOB SQL 限制

当前 `import-data` 生成 SQL 时：

- `BLOB` 使用 `HEXTORAW('<hex>')`；
- `CLOB/TEXT` 使用字符串字面量；
- 每行生成一条 `INSERT`。

如果 LOB 很大，目标库可能限制 SQL 文本长度或字面量长度。遇到这种情况，导出的 row/parts 数据仍然完整；需要后续导入执行器用分块写入或绑定变量方式装载。

## 16. 故障排查建议

### 16.1 找不到表

检查：

```sh
grep -i 'BMSQL_ORDERS' /recovery/work/dict/tab.dict
grep -i 'BMSQL_ORDERS' /recovery/work/dict/col.dict
```

如果字典中没有该表，重新执行 bootstrap，并确认 `SYSTEM.DBF` 正确。

### 16.2 导出行数为 0

检查 report：

- page plan 是否为空；
- storage_id 是否不匹配；
- root page 是否超出文件；
- 表是否实际无数据。

可以用：

```sh
./bin/dmdul inspect-page /path/to/file.dbf <root_page> --dump 128
```

### 16.3 发生 row-decode-error

使用 report 中的 `page` 和 `offset` 定位：

```sh
./bin/dmdul analyze-block /path/to/file.dbf <page_no> \
  --columns-jsonl /recovery/work/dict/col.dict \
  --max-rows 10
```

如果列定义太多，应先为目标表抽取对应列定义再分析。

### 16.4 数据中包含分隔符

默认导出为分隔文本，建议优先使用 `|` 或 `~`。如果业务字段中大量包含分隔符、换行、二进制字符或大 LOB，应使用 `--output-format row`。row 归档保留原始 DM 行内 bytes，并把 LOB payload blocks 嵌入同一个文件，避免分隔文本转义带来的歧义。

## 17. 版本验证命令

部署后建议先执行入口检查：

```sh
TMPDIR=./tmp ./bin/dmdul --help
```

当前阶段已验证：

```text
189 tests OK
```

## 18. 快速命令清单

```sh
# 1. prepare
./bin/dmdul prepare \
  --control-file /recovery/dmcopy/dm.ctl \
  --dirlist /recovery/dmcopy \
  --init-output /recovery/work/init.dul \
  --filelist-output /recovery/work/filelist.dul \
  --output-dir /recovery/work/dulout \
  --dict-dir /recovery/work/dict \
  --parallel 4 \
  --delimiter '|' \
  --json

# 2. bootstrap
./bin/dmdul --init-file /recovery/work/init.dul \
  bootstrap -b --json

# 3. dump one table
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --table BMSQL.BMSQL_ORDERS --json

# 4. dump one table as row archive
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --table BMSQL.BMSQL_ORDERS \
  --output-format row --json

# 5. dump selected partitions
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --table BMSQL.BIG_PART_T \
  --partition P_LOW,P_HIGH --output-format row --json

# 6. dump a large partitioned table with split parts
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --table BMSQL.BIG_PART_T \
  --partition-parallel 8 --output-format row --json

# 7. dump one user with workers
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --user BMSQL --parallel 8 \
  --report-output /recovery/work/dulout/bmsql_report.json \
  --json

# 8. generate reload SQL
./bin/dmdul import-data \
  --input /recovery/work/dulout/BMSQL.BMSQL_ORDERS.row \
  --output-sql /recovery/work/dulout/BMSQL.BMSQL_ORDERS.import.sql \
  --json
```
