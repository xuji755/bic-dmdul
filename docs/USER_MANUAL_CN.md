# dmdul 中文使用手册

本文档说明 `dmdul` 当前阶段的完整使用方法。`dmdul` 是一个面向达梦 DM8 数据文件的离线数据导出工具，目标是在数据库实例不能正常启动、但数据文件仍可读取的情况下，从数据文件中恢复系统字典并导出用户表数据。

当前实现重点支持：

- 识别离线数据文件清单；
- 从 `SYSTEM.DBF` bootstrap 下载系统字典；
- 从字典定位表的 storage root；
- 解析表 storage BTree root/leaf page plan；
- 导出单表或某个用户下所有表的数据；
- 对大批量表导出支持并发 worker。

当前暂不作为主流程支持：

- 达梦 ASM 磁盘组读取；
- 缺失 `SYSTEM.DBF` 时的全文件扫描重组；
- 完整 LOB 段跟随读取；
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

使用 bootstrap 字典定位表 storage root，解析 page plan，导出表数据。

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
[bootstrap] scan SYSTEM.DBF for SYSOBJECTS/SYSCOLUMNS/SYSINDEXES
[bootstrap] dictionary rows: user=1 tab=10 col=80
[bootstrap] bootstrap complete
```

这些信息用于判断长时间执行时卡在目录扫描、`SYSTEM.DBF` 扫描还是字典写入阶段。使用 `--json` 时 stdout 保持为纯 JSON，不输出这些进度行。

当前阶段 bootstrap 的核心任务：

- 找到 SYSTEM 表空间的数据文件；
- 扫描 `SYSTEM.DBF`；
- 下载关键系统字典：
  - `SYSOBJECTS`；
  - `SYSCOLUMNS`；
  - `SYSUSERS`；
  - 必要的 storage/root 信息；
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

### 7.2 导出多个指定表

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

### 7.3 导出某个用户下所有表

命令：

```sh
TMPDIR=./tmp ./bin/dmdul \
  --init-file /recovery/work/init.dul \
  dump-data \
  --user BMSQL \
  --json
```

### 7.4 并发导出

当用户下表很多时，可以启用并发：

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

- `parallel=1` 为单线程；
- `parallel>1` 会使用多个 worker；
- 并发度不宜超过磁盘实际吞吐能力；
- 大量小表可以适当提高并发；
- 大表导出主要受单表扫描和磁盘顺序读取影响。

### 7.5 导出报告

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

不使用 `--json` 时，如果有失败表，工具会把失败详情输出到 stderr，例如：

```text
table_failed=TEST2.BMSQL_ITEM
  output=/recovery/work/dulout/TEST2.BMSQL_ITEM.dul
  rows_written=0
  diagnostic=dump-data-table-failed level=error message=[Errno 2] No such file or directory: '/recovery/dmcopy/DMDUL_TS01.DBF'
```

如果看到 `tables_failed>0`，优先查看 stderr 中的 `table_failed`、`diagnostic` 和 `decode_error`。也可以同时加 `--report-output` 保存完整 JSON 报告。

### 7.6 strict page plan

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
| `page-plan-storage-id-scan` | root 结构无法直接解析，退回 storage_id scan | 需要复核，但可能仍可导出 |
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
| `CHAR` / `VARCHAR` | 支持 |
| `CLOB` / `BLOB` | 当前输出 locator/inline payload raw hex，尚未完整跟随 LOB 段 |
| 带时区时间类型 | 已有一手证据，导出策略需保留额外 TZ bytes |

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

### 13.7 复核失败表

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

`CLOB/BLOB` 当前可以导出 locator/inline payload 的 raw hex，但还没有完整跟随 LOB 段读取全部内容。

### 15.3 未知行结构

遇到未知 row metadata、NULL bitmap 变体、压缩、迁移行、行外列等情况时，当前可能产生 `row-decode-error` 或 `unsupported-row-metadata`。此时应保留 raw evidence，不得猜测导出。

### 15.4 索引

表数据导出当前不依赖独立索引段分析。表 storage 对象本身可能是 BTree 组织；独立主键/普通索引需要后续通过 `SYSINDEXES` 区分。目前不建议把表 storage BTree 和独立索引段混为一谈。

### 15.5 ASM

`init.dul` 中预留了 `diskgroups` 参数，但当前阶段暂不支持 ASM 磁盘组读取。

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

当前导出为分隔文本，建议优先使用 `|` 或 `~`。如果业务字段中也大量包含这些字符，后续需要增加 raw-safe 输出格式或字段级 raw 模式。当前原则是不为了格式美观而改写原始数据。

## 17. 版本验证命令

部署后建议先执行入口检查：

```sh
TMPDIR=./tmp ./bin/dmdul --help
```

当前阶段已验证：

```text
129 tests OK
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

# 4. dump one user with workers
./bin/dmdul --init-file /recovery/work/init.dul \
  dump-data --user BMSQL --parallel 8 \
  --report-output /recovery/work/dulout/bmsql_report.json \
  --json
```
