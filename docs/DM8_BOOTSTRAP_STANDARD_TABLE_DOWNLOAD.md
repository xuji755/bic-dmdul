# DM8 Bootstrap 与标准表下载优化设计

## 核心原则

SYSOBJECTS/SYSINDEXES 中给出的 storage root 是访问表或索引的标准入口。该入口不能被解释为不可靠地址；如果入口页不是数据页，说明还需要继续解析 root/internal/segment 结构，而不是立即退回全文件扫描。

当前已经确认的通用访问路径：

```text
SYSOBJECTS/SYSINDEXES storage root
  -> root/internal page, page_kind = 0x15
  -> child pointer, observed at offset 0x52
  -> more internal page(s), page_kind = 0x15
  -> first data/leaf page, page_kind = 0x14
  -> follow leaf next-page chain at page header offset 0x0e
```

以测试库 `TEST2.BMSQL_ITEM` 为例：

```text
root_page = 374896
storage_id = 33573582

374896 kind=0x15 storage=33573582 child@0x52=374898
374898 kind=0x15 storage=33573582 child@0x52=374832
374832 kind=0x14 storage=33573582 prev=NULL next=374833
374833 kind=0x14 storage=33573582 prev=374832 next=374834
```

因此标准表下载不需要扫描整个数据文件。它应当从 storage root 开始，递归或循环下降 internal page，找到第一个 0x14 数据页，再沿 next 链读取数据页。

## page plan 优化规则

生成 page plan 时按如下顺序处理：

1. 如果字典中已有显式 page_refs/page_numbers，优先使用，并逐页校验 page header。
2. 如果有 storage_id/root_page：
   - 读取 root_page。
   - 校验 page identity、storage_id。
   - 如果 root page 是 0x14，直接沿 leaf next 链读取。
   - 如果 root page 是 0x15，按 child pointer 下降。
   - 如果某个 internal 页不能下降，先沿 internal 页自己的 next 链尝试同层下一个 internal 页。
   - 直到找到 0x14 数据页，再沿 0x14 next 链读取。
3. 只有上述入口结构解析失败，才允许进入 root 附近局部扫描。
4. 全文件 storage_id scan 只能作为最后兜底，主要用于损坏或未知结构场景，不应作为正常路径。
5. 如果用户显式指定 `bootstrap --scan-storages-without-system-dicts`，则进入独立的无系统字典扫描模式：不尝试解析 owner/table/column 字典，只按页头 `storage_id` 聚合数据页并生成 `storage_scan.dict`。

## Bootstrap 优化方向

Bootstrap 当前不能把 SYSTEM.DBF 作为普通字节流长期全文件扫描。正确设计是：

```text
特殊入口定位 SYSOBJECTS
  -> 按标准表下载 SYSOBJECTS
  -> 从 SYSOBJECTS 中定位 SYSUSERS/SYSCOLUMNS/SYSINDEXES/SYSHPARTTABLEINFO 等字典表
  -> 每张字典表继续按标准表下载
  -> 生成 user.dict/tab.dict/col.dict/index.dict/partition.dict 等
```

也就是说，只有 SYSOBJECTS 的入口定位是 bootstrap 特殊逻辑。一旦 SYSOBJECTS 被下载成功，其余字典表不应再通过 marker 全文件扫描获取，而应使用 SYSOBJECTS 中的对象与 storage 子对象信息，进入标准表下载流程。

当前实现已经把 `SYSOBJECTS`/`SYSINDEXES` 的固定 root page 和固定 storage
id 都降级为兜底。DM7/DM8 对比确认 SYSTEM.DBF page 0 存在 bootstrap-like
入口：offset `0x80` 保存 `SYSOBJECTS` root page，offset `0x7c` 保存
`SYSINDEXES` root page。读取这些 root 页后，再从普通 page header 中取得
storage id，并按标准 BTREE 路径下载字典行。`SYSCOLUMNS` root 由离线解码
后的 `SYSINDEXES` 行给出。

如果 `SYSTEM.DBF` 丢失或上述 SYS 字典入口无法读取，普通 `bootstrap -b`
必须返回错误并写入 diagnostics，例如 `bootstrap-system-file-not-found`。
此时不能继续把空的 `tab.dict/col.dict` 当作正常字典使用。

显式降级路径为：

```text
bootstrap --scan-storages-without-system-dicts
  -> 扫描 file.dict 中所有 DBF 页头
  -> 按 (group_id, file_no, storage_id) 聚合 page_kind=0x14 数据页
  -> 写 storage_scan.dict
  -> 在 tab.dict 中写 SCAN.TAB_<storage_id> 占位对象
  -> dump-data --scan-storage-dict 按 raw_row VARBINARY 导出完整物理行 bytes
```

这个模式不恢复真实 owner、表名、列名和字段类型。它只用于 SYSTEM 字典不可用时先保全物理行和样本，后续再靠字段列表或 raw 字典残留做结构化恢复。

## 已确认的核心字典对象入口证据

在当前 DM8 测试库中，在线字典、离线页头发现和 DM7/DM8 page 0 对比确认如下核心对象关系：

```text
SYSOBJECTS        table object id = 0
SYSINDEXES        table object id = 1
SYSCOLUMNS        table object id = 2
SYSHPARTTABLEINFO table object id = 19

SYSINDEXSYSOBJECTS        storage/index id = 33554540 root = group 0 file 0 page 16
SYSINDEXCOLUMNS           storage/index id = 33554433 root = group 0 file 0 page 80
SYSINDEXINDEXES           storage/index id = 33554434 root = group 0 file 0 page 288
SYSINDEXSYSHPARTTABLEINFO storage/index id = 33554548 root = group 0 file 0 page 240
```

其中 `SYSOBJECTS` 的表对象 ID 是 bootstrap 的根。实现上应优先通过 SYSTEM
page 0 的 bootstrap-like 入口定位 `SYSOBJECTS` root，再从 root page header
得到 storage id，并按标准表下载路径读取 SYSOBJECTS；不要把扫描
SYSTEM.DBF 字符串 marker 作为主路径。

## SYSOBJECTS 之后的字典下载流程

下载 SYSOBJECTS 后：

1. 在 SYSOBJECTS 行中查找需要的字典表对象：
   - SYSUSERS
   - SYSCOLUMNS
   - SYSINDEXES
   - SYSHPARTTABLEINFO
   - 其他后续需要的系统表
2. 对每张字典表查找对应 storage 子对象。
3. 获取 storage_id、group_id、file_id、root_page。
4. 复用标准 page plan：root -> internal -> leaf -> next chain。
5. 解码该字典表行，并写入平面 `.dict` 文件。

## 禁止的正常路径

正常 bootstrap 或 dump-data 不应把以下操作作为主路径：

```text
扫描整个 DBF 文件寻找对象名字符串
扫描整个 DBF 文件寻找 storage_id
扫描整个 SYSTEM.DBF 寻找 SYSCOLUMNS/SYSINDEXES marker
```

这些只能作为最后 fallback，用于 SYSTEM 损坏、入口页损坏、字典不完整等异常场景。

例外：`--scan-storages-without-system-dicts` 是用户显式请求的灾难恢复路径，不属于正常 bootstrap/dump-data 主路径。该模式的输出文件为 `storage_scan.dict` 和 `SCAN.TAB_<storage_id>` raw 导出入口。
