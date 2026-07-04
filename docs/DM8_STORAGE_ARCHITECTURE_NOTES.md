# DM8 Storage Architecture Notes

This document records verified observations and working hypotheses for the
offline `bic-dmdul` extractor.

## Scope Boundary

Dictionary views are useful during research, but the final extractor cannot
depend on them because the target database may not start. The extractor must
recover equivalent metadata directly from dictionary tables and data files.

Online views are therefore used only as calibration data.

## Logical Storage Model

Knowledge graph and live tests agree on this hierarchy:

```text
database -> tablespace -> data file -> segment -> extent/cluster -> page
```

Observed instance parameters:

- page size: `8192` bytes
- extent/cluster size: `16` pages by default

The test tablespace `DMDUL_TS` has:

- group/tablespace id: `6`
- data file: `/dmdata/data/DAMENG/DMDUL_TS01.DBF`
- one file with `8192` pages

## Online View To Dictionary Table Mapping

The Oracle-compatible DBA views are not the storage source of truth. Their view
definitions point to lower-level SYS objects.

Important findings:

- `DBA_TABLES` is built from `SYS.SYSOBJECTS` and related tables/views.
- `DBA_SEGMENTS` and `DBA_EXTENTS` use `SYS.SYSINDEXES` heavily.
- `DBA_DATA_FILES` is built from `SYS.V$DATAFILE` and `V$TABLESPACE`.
- `DBA_TABLESPACES` is built from `V$TABLESPACE`, `V$HUGE_TABLESPACE`, and
  related runtime/system metadata.

Core dictionary tables for first-stage offline recovery:

| SYS object | Purpose | Key fields |
| --- | --- | --- |
| `SYS.SYSOBJECTS` | object names, ids, schema ids, object type/subtype | `NAME`, `ID`, `SCHID`, `TYPE$`, `SUBTYPE$`, `PID`, `INFO1..INFO8`, `VALID` |
| `SYS.SYSCOLUMNS` | column definitions | `NAME`, `ID`, `COLID`, `TYPE$`, `LENGTH$`, `SCALE`, `NULLABLE$`, `DEFVAL` |
| `SYS.SYSINDEXES` | BTREE/segment roots and allocation metadata | `ID`, `ISUNIQUE`, `GROUPID`, `ROOTFILE`, `ROOTPAGE`, `TYPE$`, `XTYPE`, `FLAG`, `KEYNUM`, `KEYINFO`, `INIT_EXTENTS`, `BATCH_ALLOC`, `MIN_EXTENTS` |
| `SYS.V$DATAFILE` | data file metadata source used by `DBA_DATA_FILES` | `GROUP_ID`, `ID`, `PATH`, `TOTAL_SIZE`, `FREE_SIZE`, `FREE_PAGE_NO`, `PAGE_SIZE`, status fields |

The offline extractor should first recover `SYSOBJECTS`, `SYSCOLUMNS`, and
`SYSINDEXES` from `SYSTEM.DBF`, then use their content to locate user tables.

## Dictionary Table Physical Locations

On the test instance:

| Dictionary table | Tablespace | Root/header block | Allocated blocks |
| --- | --- | ---: | ---: |
| `SYS.SYSOBJECTS` | `SYSTEM` | 16 | 784 |
| `SYS.SYSCOLUMNS` | `SYSTEM` | 80 | 96 |
| `SYS.SYSINDEXES` | `SYSTEM` | 288 | 320 |

With 8KB pages, these map to offsets:

- `SYSOBJECTS`: `16 * 8192 = 131072`
- `SYSCOLUMNS`: `80 * 8192 = 655360`
- `SYSINDEXES`: `288 * 8192 = 2359296`

These locations are currently known from online views. A later bootstrap step
must discover them from file headers or fixed system metadata, not from views.

## File Header And Early Pages

Observed first pages of `DMDUL_TS01.DBF`:

| Page | Observation |
| ---: | --- |
| 0 | nonzero file header/control page; starts with group id `6`, page id `0` |
| 1 | nonzero control/metadata page |
| 2 | zero page |
| 3 | nonzero control/metadata page |
| 4-7 | zero pages |
| 8 | nonzero space-management or bitmap-like page |
| 9-15 | mostly zero in sampled range |
| 16 | first test table/root page |

This supports the working model that ordinary object allocation starts at page
16 in a new data file, with earlier pages reserved for file-level metadata and
space management.

`SYSTEM.DBF` has richer early metadata:

- page 0 is a file header/control page;
- pages 1 and 2 are nonzero metadata pages;
- page 16 is the `SYSOBJECTS` root/header page.

## Observed Page Header Layout

Across `SYSTEM.DBF` and `DMDUL_TS01.DBF`, the first bytes of nonzero pages follow
a stable pattern:

| Offset | Size | Working name | Observation |
| ---: | ---: | --- | --- |
| `0x00` | 1 | `page_type_raw` | first page byte; likely related to the real PAGE type and must be calibrated separately |
| `0x00` | 4 | `group_raw` | legacy observed split; low 16 bits appear to be tablespace/group id and high 16 bits appear to be file-number hint in current samples, but this overlaps `page_type_raw` |
| `0x04` | 4 | `page_no` | zero-based page number inside the data file |
| `0x08` | 6 | `prev_page_ref` | 6-byte page reference or all `ff` for null |
| `0x0e` | 6 | `next_page_ref` | 6-byte page reference or all `ff` for null |
| `0x14` | 4 | `page_kind_raw` | observed page role/classification field used for current evidence labels; not assumed to be the real PAGE type |
| `0x20` onward | variable | page-kind-specific fields | counters, free offsets, row counts, object ids, etc. |

The 6-byte page reference appears to be:

```text
u16 file_no + u32 page_no
```

Examples:

- `ff ff ff ff ff ff` => null pointer
- `00 00 60 00 00 00` => file 0, page 96
- `00 00 62 00 00 00` => file 0, page 98

The page-header first field was initially treated as only `group_id`. Real
multi-file tablespaces showed this was incomplete, and the first byte was
recorded independently as `page_type_raw` because DM PAGE type is commonly
stored at the beginning of the page. Later SYSTEM/user-file scans refined that
interpretation: in the tested files, the first byte is the low byte of
`group_raw`, not a reliable page type. `SYSTEM.DBF` has first byte `0x00` on
all nonzero pages because its group id is `0`; `DMDUL_TS01.DBF` has first byte
`0x06` on all nonzero pages because its group id is `6`. The current page-role
candidate remains the 4-byte `page_kind_raw` field at offset `0x14`.

| File | Raw first field | Working split |
| --- | ---: | --- |
| `MAIN.DBF` | `0x00000004` | file hint 0, group id 4 |
| `main2.dbf` | `0x00010004` | file hint 1, group id 4 |
| `main3.dbf` | `0x00020004` | file hint 2, group id 4 |
| `SYSAWR02.DBF` | `0x00010005` | file hint 1, group id 5 |

Working rule:

```text
group_id = group_raw & 0xffff
file_no_hint = group_raw >> 16
```

`catalog-pages` and database summaries now also emit cross-counts between the
first byte and the observed role field:

- `page_type_counts`: count by first byte, such as `0x06`;
- `page_type_kind_counts`: for each first byte, the observed `page_kind_raw`
  values seen with it;
- `page_kind_type_counts`: for each observed role field, the first-byte values
  seen with it.

These matrices are the current evidence path for deriving a preliminary
first-byte PAGE type enum from known page classes. Zero-filled pages are counted
as `zero` rather than as `0x00` so they do not pollute real type calibration.

`TEMP.DBF` also starts with low group id 0, but its observed role field at page
0 was `0x0`, not `0x13`. A SYSTEM-file candidate therefore currently requires
group id 0, page number 0, and observed role `0x13`.

Common `page_kind_raw` values observed at offset `0x14`:

| Raw value | Current label | Pages | Working meaning |
| ---: | --- | --- | --- |
| `0x13` | `tentative-file-control` | data file page 0/1 | file/control page |
| `0x11` | `tentative-space-bitmap` | data file page 8 | space-management/bitmap-like page |
| `0x14` | `tentative-btree-data` | small table root pages and data leaf pages | BTREE/data page |
| `0x15` | `tentative-segment-root` | larger root/header pages | segment root/header candidate |
| `0x1a1a001a` | `tentative-internal-metadata` | companion/internal pages after root page | internal/metadata page candidate |
| `0xffff00ff` | `tentative-empty-initialized` | empty initialized pages | empty/free page candidate |

These names are tentative labels used in `ObservedPageHeader` and
`catalog-pages` output. They are not final parser semantics.

### Page Header Storage Id And Change Fields

Scanning `evidence/system/SYSTEM.DBF` and
`evidence/type_store/DMDUL_TS01.DBF` produced these stronger working
interpretations for ordinary nonzero pages:

| Offset | Size | Working name | Evidence |
| ---: | ---: | --- | --- |
| `0x18` | 4 | `page_checksum_or_hash_candidate` | changes per page and appears non-monotonic when interpreted as `u32le` |
| `0x1c` | 4 | `page_change_scn_candidate` | the `u32le` high half of the 8-byte field at `0x18`; user-table BTREE pages show monotonic-looking values across linked/related pages |
| `0x38` | 2 | `storage_prefix_candidate` | root/header pages may differ from leaf pages; exact meaning still unknown |
| `0x3a` | 4 | `storage_id_candidate` | matches `SYS.SYSINDEXES.ID` and the `SYS.SYSOBJECTS` table-level `TABOBJ/INDEX` child object id |

The 8-byte field at `0x18` should no longer be treated as one opaque SCN-like
`u64`. In user-table BTREE pages, the low `u32` at `0x18` has hash-like or
checksum-like behavior, while the high `u32` at `0x1c` has stronger page-change
or SCN-like behavior. This still needs synchronized checkpoint/DML evidence
before it can be promoted to a real SCN field.

The storage id evidence is stronger. For `DMDUL_MANY`, the table object id is
`33629`, but the root and leaf pages contain storage id `33595349` at
`0x3a`:

```text
page 80  offset 0x38: 0100d59f0002 => prefix 1, storage id 33595349
page 96  offset 0x38: 0000d59f0002 => prefix 0, storage id 33595349
page 135 offset 0x38: 0000d59f0002 => prefix 0, storage id 33595349
```

Other controlled pages show the same relation:

```text
page 144 offset 0x38: 0000d69f0002 => storage id 33595350
page 160 offset 0x38: 0000d79f0002 => storage id 33595351
page 176 offset 0x38: 0000d89f0002 => storage id 33595352
page 192 offset 0x38: 0000d99f0002 => storage id 33595353
page 208 offset 0x38: 0000da9f0002 => storage id 33595354
```

The field therefore identifies the storage object, not the table object.
For page ownership checks, compare page-header `storage_id_candidate` against
`SYSINDEXES.ID`, not against `SYSOBJECTS.ID` for the parent table.

## Segment And Extent Findings

`DBA_SEGMENTS` reports allocated segment blocks. For example:

| Segment | Type | Root/header block | `DBA_SEGMENTS.BLOCKS` |
| --- | --- | ---: | ---: |
| `DMDUL_T1` | table | 16 | 16 |
| `DMDUL_HEAP` | table | 48 | 16 |
| `DMDUL_TYPES` | table | 64 | 16 |
| `DMDUL_MANY` | table | 80 | 64 |
| `DMDUL_EXT2` | table | observed from `SYSINDEXES`/segment manifest | 880 |

`DBA_EXTENTS` reported smaller `BLOCKS` values in tests:

| Segment | Root/header block | `DBA_EXTENTS.BLOCKS` |
| --- | ---: | ---: |
| `DMDUL_T1` | 16 | 2 |
| `DMDUL_HEAP` | 48 | 2 |
| `DMDUL_TYPES` | 64 | 2 |
| `DMDUL_MANY` | 80 | 2 |

Working interpretation:

- `DBA_SEGMENTS.BLOCKS` reflects allocated cluster/page capacity.
- `DBA_EXTENTS.BLOCKS` may reflect currently used or registered pages for the
  extent in the compatibility view, not the full allocated 16-page cluster.
- `SYS.SYSINDEXES.ROOTFILE/ROOTPAGE/GROUPID` is closer to the physical source
  needed by an offline extractor.

A later 25,000-row controlled table, `DMDUL_EXT2`, forced allocation beyond one
extent. `DBA_SEGMENTS` reported `BLOCKS=880` and `EXTENTS=55`, while
`DBA_EXTENTS` on the same environment returned only one visible row
(`EXTENT_ID=24`, `BLOCKS=4`). That result is useful calibration evidence: the
online compatibility view should not be assumed to expose a complete Oracle-like
per-extent map. Offline recovery must derive allocation and traversal from
dictionary table rows, segment/root pages, extent/space metadata, and validated
page-header ownership instead of depending on `DBA_EXTENTS` semantics.

## Table Organization

Every ordinary user table observed has an associated `SYS.SYSINDEXES` BTREE-like
entry with:

- `TYPE$ = 'BT'`
- `ROOTFILE = 0`
- `ROOTPAGE = table root/header block`
- `GROUPID = tablespace id`


The offline table-entry chain now has a clearer evidence-backed shape:

```text
SYSOBJECTS table object row
  TYPE$='SCHOBJ', SUBTYPE$ in ('UTAB', 'STAB')
  ID = table_object_id

SYSOBJECTS table-level child row
  TYPE$='TABOBJ', SUBTYPE$='INDEX'
  PID = table_object_id
  ID = storage_id
  NAME = INDEX<storage_id>

SYSINDEXES row
  ID = storage_id
  GROUPID, ROOTFILE, ROOTPAGE = physical entry

PAGE HEADER
  u32le(page[0x3a:0x3e]) = storage_id, used to validate page ownership
```

`SYSOBJECTS` table object rows contain the table identity (`NAME`, `ID`,
`SCHID`, `TYPE$`, `SUBTYPE$`) but currently do not show direct
`GROUPID/ROOTFILE/ROOTPAGE` values. Those physical entry fields are recovered
from `SYSINDEXES` by way of the `TABOBJ/INDEX` child object.

Even a table without a primary key (`DMDUL_HEAP`) has a BTREE storage object.
This supports the initial assumption that ordinary DM row tables are BTREE
organized at the storage layer.

For primary-key table `DMDUL_T1`, an additional unique index has its own root
page at 32.

## Header Block Versus Row Location

Small tables:

| Table | Root/header page | First marker | File offset | Page offset |
| --- | ---: | --- | ---: | ---: |
| `DMDUL_T1` | 16 | `DMDUL_ROW_0001` | 131202 | 130 |
| `DMDUL_HEAP` | 48 | `HEAP_ROW_0101` | 393322 | 106 |
| `DMDUL_TYPES` | 64 | `VARCHAR_LENGTH_020` | 524539 | 251 |

Rows are stored directly in the root/header page for these tiny tables.

Multi-page table:

| Table | Root/header page | Marker | File offset | Page |
| --- | ---: | --- | ---: | ---: |
| `DMDUL_MANY` | 80 | `MANY_ROW_1` | 786538 | 96 |
| `DMDUL_MANY` | 80 | `MANY_ROW_80` | 1109066 | 135 |

For larger data, the root/header page remains at 80 but row data moved to leaf
pages, starting at page 96 in this sample. Pages 96, 97, and later leaf pages
are linked by the observed 6-byte page references.

A calibrated page-range scan of `DMDUL_MANY` over pages 80..143 decoded all 80
expected rows directly from `/dmdata/data/DAMENG/DMDUL_TS01.DBF`:

```text
decoded_rows 80
first [(96, 1, 'MANY_ROW_1', 3000), (96, 2, 'MANY_ROW_2', 3000), ...]
last  [(133, 76, 'MANY_ROW_76', 3000), ..., (135, 80, 'MANY_ROW_80', 3000)]
ids_min_max (1, 80)
```

This validates the current transitional extraction strategy for non-NULL
`INT, VARCHAR, VARCHAR` rows when the segment page range is supplied through
calibrated metadata. The final extractor still needs to derive the range from
offline dictionary/BTREE metadata instead of requiring it in JSON.

`DMDUL_EXT2` validates the same extraction path at a larger allocation size:
25,000 rows were extracted with `strict_ok=true` and no decode errors, and the
CSV passed aggregate and per-row formula checks (`ID=1..25000`,
`sum(ID)=312512500`, `BUCKET=ID mod 997`, `MARKER='EXT2_'||ID`, exact `PAD`
payload). This table also exposed a page-plan diagnostic bug: a segment-root
entry may point to an internal child whose descendant leaf pages are already
covered by the walked leaf next-chain. The root-entry coverage check now treats
that branch as covered instead of reporting a false
`page-plan-btree-root-entry-mismatch`.

## Storage-Id Scan Recovery When SYSTEM Is Missing

Because ordinary table and index pages carry `storage_id_candidate` in the page
header, a damaged or missing `SYSTEM.DBF` does not make all physical recovery
impossible. Without SYS dictionary rows, names and column definitions are not
reliably available, but the extractor can still group data pages by storage
object:

```text
scan all DBF files
  -> read nonzero page headers
  -> group pages by u32le(page[0x3a:0x3e])
  -> classify root/header, internal, leaf/data, empty, and metadata pages
  -> follow same-storage next-page links where available
  -> emit storage_scan.dict and SCAN.TAB_<storage_id> physical recovery candidates
```

This fallback is now implemented as
`bootstrap --scan-storages-without-system-dicts` and
`dump-data --scan-storage-dict`. It can recover page ownership, root/header
candidates, linked leaf chains, approximate row counts, and raw row samples for
each storage object. It cannot by itself recover schema/table names, column
names, exact types, or full transaction visibility semantics. Treat it as a
second recovery route and a cross-check for dictionary-driven recovery.

## Row Format Observations

Rows are stored inline and generally in little-endian format for binary numeric
types.

The first two bytes of a row appear to store row length and status bits:

- normal row example: `00 25` => length `0x25`;
- deleted row example: `80 27` => deleted flag plus length `0x27`;
- working rule: `length = u16be & 0x7fff`, `deleted = (u16be & 0x8000) != 0`.

The current observed row-layout model used by the extractor is:

| Region | Size | Status |
| --- | ---: | --- |
| row length/status | 2 bytes | decoded as above |
| observed row metadata | `ceil(column_count/4)` bytes | two-bit NULL state per storage-order column in controlled samples |
| user column payload | variable | decoded by column type |

For controlled ordinary rows, the metadata is interpreted little-endian with two
bits per storage-order column. Storage order means fixed-width columns first,
then variable-width columns, while SQL output is restored to dictionary column
order. Observed states are `00` for present and `11` for NULL. Fixed-width NULL
columns still reserve their fixed-width bytes. Variable-width NULL columns omit
the variable-length prefix and payload. Metadata states `01` and `10`, and any
extra bits beyond the column count, remain unsupported and are reported as
`unsupported-row-metadata`.

`catalog-pages` now includes a `row_area_probe` object in each nonzero page
sample. The probe starts at the currently observed row-chain offset `0x62`,
walks row-length prefixes with the same conservative scanner used by extraction,
and records:

- `header_observed_row_count` from the page-header field currently exposed as
  `field_2c_u16le`;
- physical, live, and deleted row counts found in the row-length chain;
- `count_delta_physical_minus_header`, which highlights pages where deleted or
  updated row records remain physically present after the header count changes;
- sampled row offsets, lengths, and deleted flags.

This is evidence for calibrating the row count, free-space boundary, and slot
directory fields. It is not yet a complete slot-directory decoder.

The catalog also includes a top-level `row_area_summary` that currently
aggregates only pages labeled `tentative-btree-data`. It reports how many such
pages had physical rows, deleted rows, and row-count deltas, plus a histogram
and capped page samples for count-delta and deleted-row cases. Non-data pages
still keep their per-page probe in `nonzero_samples`, but they are excluded from
the aggregate so file-control and space-management pages do not pollute row
format calibration.

The row-area probe also records neutral candidate relations for anonymous page
header fields `field_20_u32le`, `field_24_u16le`, `field_26_u16le`, and
`field_2c_u16le`. For each field it records whether the value equals observed
quantities such as row-chain start offset, row-chain end offset, header row
count, physical row count, live row count, or deleted row count. The aggregate
`header_field_relation_counts` shows which relations are stable across sampled
BTREE/data pages. These relations are intended to identify row count, free-space
boundary, and slot-directory fields from evidence before assigning final names.

The same probe records a `slot_tail_probe` for the bytes after the observed row
chain. It counts nonzero bytes in that tail region, scans 2-byte little-endian
candidate values without assuming alignment, and records whether each value
points to a row start discovered by the physical row-chain scan. The
`row_area_summary` aggregates candidate counts and row-start hits for
BTREE/data pages. This is only a slot-directory candidate detector; real slot
ordering, deleted-slot handling, and free-space compaction still need controlled
fixtures.

## Data-Page Transaction State Open Points

There is not yet enough evidence to claim that DM8 data pages contain an
Oracle-style block-level ITL/transaction-slot array. The current evidence proves
that a page-tail row indicator list exists, but those entries are 2-byte row
offsets, not decoded transaction slots.

Transaction/MVCC state is still an open area. The strongest current candidate is
the 19-byte row tail/control region observed after decoded column payloads. Clean
inserted rows often carry a pattern containing `ff ff ff ff 7f ff ff`, while the
delete/update sample on page `208` changes this region in rows affected by DML:

| Row state | Row offset | Row tail/control bytes |
| --- | ---: | --- |
| live keep row | 98 | `01 00 00 00 00 00 ff ff ff ff 7f ff ff 31 d7 34 04 00 00` |
| committed deleted row | 135 | `02 00 00 00 00 00 00 01 13 00 00 6a 00 32 d7 34 04 00 00` |
| live updated-after row | 174 | `03 00 00 00 00 00 00 01 13 00 00 97 00 32 d7 34 04 00 00` |

This looks transaction-related, possibly containing row ordinal/status plus a
transaction id, commit marker, undo address, or row-version linkage, but the
exact semantics are not decoded. It must not be treated as a complete visibility
decision yet.

The remaining unresolved data-page fields include:

- page-header fields after `0x20`: active slot count, free-space boundaries,
  physical-row boundary, object or segment identity, page change counters,
  SCN/LSN/checkpoint fields, and checksum/validation fields;
- complete slot-directory metadata: slot count, slot base, ordering, reusable
  deleted slots, and compaction behavior;
- row-head flags beyond length/deleted bit: lock flag, update-chain marker,
  committed/uncommitted state, NULL metadata, and possible variable-column
  directory flags;
- row tail/control bytes: whether they contain transaction status, undo pointer,
  commit visibility state, or row version-chain linkage;
- mapping from any row/page transaction reference to rollback/undo files and
  undo records.

To confirm or reject an Oracle-like transaction-slot model, the next controlled
fixtures must compare the same page while a transaction is still open:
uncommitted insert, uncommitted delete, uncommitted update, rollback, commit
after checkpoint, and slot reuse after delete. The comparison must include page
header bytes, slot-tail bytes, row-head flags for both committed and
uncommitted rows, row tail/control bytes, and the corresponding ROLL/undo file
pages.

## Unknown-Structure Dump Findings

The `dump-unknown-structures` command emits currently anonymous regions as raw
hex plus 8/16/24-byte chunk interpretations:

```bash
PYTHONPATH=src python3 -m dmdul.cli dump-unknown-structures \
  evidence/type_store/DMDUL_TS01.DBF \
  --pages 144,160,176,192,208,224,288 \
  --output /tmp/dmdul_unknown_control_pages.json
```

The first controlled run shows that the anonymous data is not random. Several
fields line up across BTREE/data pages:

| Page | `0x24` u16 | `0x26` u16 | active rows `0x2c` u16 | `0x2e` u16 | physical rows | row chain end |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 144 | 6 | 202 | 4 | `ffff` | 4 | 202 |
| 160 | 6 | 270 | 4 | `ffff` | 4 | 270 |
| 176 | 10 | 2097 | 8 | `ffff` | 8 | 2097 |
| 192 | 5 | 224 | 3 | `ffff` | 3 | 224 |
| 208 | 4 | 220 | 2 | 135 | 3 | 219 |
| 224 | 6 | 359 | 4 | `ffff` | 4 | 359 |
| 288 | 5 | 862 | 3 | `ffff` | 3 | 862 |

Working interpretations to validate:

- `0x24` looks like active row count plus two page/control rows in these samples.
- `0x26` tracks the end of the physical row area or the next free offset. Page
  `208` differs by one byte because the committed-delete/update sample has a
  stray nonzero byte immediately after the physical row chain.
- `0x2c` is the active row/slot count.
- `0x2e` is `ffff` on normal pages and equals deleted-row offset `135` on page
  `208`; it is a strong free/deleted-row-list candidate.

The 24-byte windows in the page header show repeatable structure, not a single
opaque blob. Examples:

```text
page 208 offset 0x18:
cb4ba7605081a5a4 05000000 0400dc00 00000000 02008700

page 224 offset 0x18:
4333e4e280eaa9a4 05000000 06006701 00000000 0400ffff

page 288 offset 0x18:
625e4fad36e5a9a4 05000000 05005e03 00000000 0300ffff
```

The first 8 bytes at `0x18` change per page and are a candidate page-change
number, checksum, or SCN-like value, but no ordering rule is proven yet. The
next fields align with the counter/free-list candidates above.

Another stable header candidate appears at offset `0x38` as a 6-byte
little-endian reference-like value:

```text
page 144 offset 0x38: 0000d69f0002 => file 0, value 33595350
page 160 offset 0x38: 0000d79f0002 => file 0, value 33595351
page 176 offset 0x38: 0000d89f0002 => file 0, value 33595352
page 192 offset 0x38: 0000d99f0002 => file 0, value 33595353
page 208 offset 0x38: 0000da9f0002 => file 0, value 33595354
```

For these pages, the value matches the table storage/index identifier observed
from dictionary evidence, so `0x38` is a strong object/storage-id candidate.

The row tail/control region is 19 bytes in current samples, not 24 bytes. It has
two obvious substructures:

```text
normal row:  <row ordinal u48le> ff ff ff ff 7f ff ff <6-byte candidate> 00
DML row:     <row ordinal u48le> 00 01 13 00 00 <row/page-ish value> <6-byte candidate> 00
```

The final 6-byte candidate at relative offset `12` inside the row tail changes
regularly and may be SCN-like or transaction-related:

| Page | Row tail final candidate |
| ---: | --- |
| 144 | `ff29d7340400` |
| 160 | `ff2bd7340400` |
| 176 | `ff2dd7340400` |
| 192 | `ff2fd7340400` |
| 208 live keep row | `ff31d7340400` |
| 208 committed deleted/updated rows | `0032d7340400` |
| 224 | `ffb042350400` |
| 288 | `ffb941350400` |

The controlled pages `144..208` show a monotonic-looking sequence in this field.
The delete/update page then shows affected DML rows with the next adjacent
candidate value. This is the best current SCN-like candidate, but it must be
validated against explicit transaction timestamps/SCN sources and uncommitted
DML samples before being used for visibility decisions.

An online query later returned:

```sql
SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER();
-- 24237442891, hex 0x5a4aa074b
```

The direct byte encodings of this value were:

```text
le6 4b07aaa40500
le8 4b07aaa405000000
be6 0005a4aa074b
be8 00000005a4aa074b
```

None of these byte patterns, nor the low 32-bit forms, were found in the copied
`DMDUL_TS01.DBF` evidence file. The row-tail candidates are also not a direct
numeric match:

| Candidate bytes | u48 little-endian value | Delta from online SCN |
| --- | ---: | ---: |
| `ff29d7340400` | 18066385407 | -6171057484 |
| `ff31d7340400` | 18066387455 | -6171055436 |
| `0032d7340400` | 18066387456 | -6171055435 |
| `ffb042350400` | 18073432319 | -6164010572 |
| `ffb941350400` | 18073369087 | -6164073804 |

This does not disprove an SCN relationship because the DBF copy and the online
SCN query were not captured as one synchronized checkpoint experiment. It does
mean the current evidence cannot claim that the row-tail candidate is the direct
stored value of `DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER()`. The next required
test is to query the SCN, force checkpoint, copy the relevant data file, and
search/dump the same page set immediately, then repeat after one controlled DML.

Observed fixed-width values:

- `INT 1` -> `01 00 00 00`
- `INT 11` -> `0b 00 00 00`
- `INT -1` -> `ff ff ff ff`
- `INT -222` -> `22 ff ff ff`
- `BIGINT 1111111111` -> `c7 35 3a 42 00 00 00 00`
- `BIGINT 2222222222` -> `8e 6b 74 84 00 00 00 00`
- `DOUBLE 1.5` -> `00 00 00 00 00 00 f8 3f`
- `DOUBLE 2.25` -> `00 00 00 00 00 00 02 40`
- `DOUBLE -1.5` -> `00 00 00 00 00 00 f8 bf`
- `DOUBLE -2.25` -> `00 00 00 00 00 00 02 c0`

Variable-length fields use inline payload plus a compact length marker. Working
examples:

- `HEAP_ROW_0101` is preceded by `8d`; length is 13, so `0x80 + len` is likely
  used for short VARCHAR values.
- `V1` is preceded by `82`; length is 2.
- `VARCHAR_LENGTH_020` is preceded by `92`; length is 18.
- `MANY_ROW_1` is preceded by `8a`; length is 10.
- The 3000-byte `PAD` value in `DMDUL_MANY` is preceded by `0b b8`, which is
  decimal 3000. This suggests longer VARCHAR values use a multi-byte length
  form.

More controlled `DMDUL_VLEN2` tests show the threshold more clearly:

| Value length | Prefix | Interpretation |
| ---: | --- | --- |
| 1 | `81` | `0x80 + 1` |
| 2 | `82` | `0x80 + 2` |
| 10 | `8a` | `0x80 + 10` |
| 127 | `ff` | `0x80 + 127` |
| 128 | `00 80` | two-byte big-endian length |
| 255 | `00 ff` | two-byte big-endian length |
| 256 | `01 00` | two-byte big-endian length |
| 1000 | `03 e8` | two-byte big-endian length |

Controlled `DMDUL_NULL2` rows decoded the observed NULL metadata rule: two
little-endian bits per storage-order column, `00` for present and `11` for NULL.
For `ID INT, A INT, B VARCHAR, C BIGINT, D VARCHAR`, storage order is
`ID, A, C, B, D`; for example metadata `3c 00` decodes `A` and `C` as NULL
while `B` and `D` are present.

Date/time/timestamp encodings are also not fully decoded yet. They appear as
compact binary values, not text. More boundary-value tests are needed.

Delete/update observations from `DMDUL_MOD2`:

- `MOD_DELETE_2` remains in the page after delete.
- The deleted row's first two bytes changed to a value with high bit set
  (`80 27` in the observed sample).
- `MOD_UPDATE_3_BEFORE` was no longer found after update.
- `MOD_UPDATE_3_AFTER` was present in the same root page.
- A later page read reported page-header row count `2` for `DMDUL_MOD2`, while
  the physical row chain still contained three row-like records:
  - live `MOD_KEEP_1`
  - deleted `MOD_DELETE_2`
  - live `MOD_UPDATE_3_AFTER`

This means the page header count should be treated as active-row count or slot
count, not as a physical row-chain limit. The extractor's early scan path should
walk the physical row-length chain and skip rows whose delete flag is set.

## Additional Controlled Tables

The following focused tables were added after the initial batch:

| Table | Purpose | Root/header page |
| --- | --- | ---: |
| `DMDUL_ONE2` | one-column integer row boundaries | 144 |
| `DMDUL_NULL2` | NULL combinations | 160 |
| `DMDUL_VLEN2` | VARCHAR length thresholds | 176 |
| `DMDUL_DTTM2` | DATE/TIME/TIMESTAMP boundaries | 192 |
| `DMDUL_MOD2` | delete/update row flags | 208 |

Their corresponding storage BTREE rows in `SYS.SYSINDEXES` are:

| Table | Storage index id | Group id | Root file | Root page |
| --- | ---: | ---: | ---: | ---: |
| `DMDUL_ONE2` | 33595350 | 6 | 0 | 144 |
| `DMDUL_NULL2` | 33595351 | 6 | 0 | 160 |
| `DMDUL_VLEN2` | 33595352 | 6 | 0 | 176 |
| `DMDUL_DTTM2` | 33595353 | 6 | 0 | 192 |
| `DMDUL_MOD2` | 33595354 | 6 | 0 | 208 |

Their `SYS.SYSOBJECTS` rows have:

- object ids: `33630..33634`;
- schema id: `150994945`;
- `TYPE$ = SCHOBJ`;
- `SUBTYPE$ = UTAB`;
- `INFO1 = 2097152`;
- `INFO3 = 4503599627436032`.

## Dictionary Row Observations

Searching `SYSTEM.DBF` for new object names found multiple occurrences. One
important occurrence group around file offset `6001516` contains compact rows
with inline object-name and object-type strings:

```text
... 09 8a 44 4d 44 55 4c 5f 4f 4e 45 32
    86 53 43 48 4f 42 4a
    8a 44 4d 44 55 4c 5f 4f 4e 45 32 ...
```

Working interpretation:

- `8a DMDUL_ONE2` is a short string with length 10.
- `86 SCHOBJ` is a short string with length 6.
- The same variable-length string encoding is used by SYS dictionary rows.

Another occurrence around offset `29420762` is a wider row containing
`DMDUL_ONE2`, `SCHOBJ`, and `UTAB`, likely closer to the base `SYSOBJECTS` row
or a related BTREE representation. This region includes known values such as
object id `33630` and schema id `150994945`, but exact column offsets still need
to be decoded.

The tool now has a heuristic `find-sysobject` command that scans `SYSTEM.DBF`
for an object name and scores nearby contexts:

```sh
bic-dmdul find-sysobject SYSTEM.DBF DMDUL_ONE2
```

Current scoring favors contexts containing:

- the target object name with observed variable-length prefix;
- `SCHOBJ`;
- `UTAB`;
- nearby plausible little-endian object ids.

Remote validation against the test `SYSTEM.DBF` showed the highest-scoring
`DMDUL_ONE2`, `DMDUL_MANY`, and `DMDUL_MOD2` candidates are the wide contexts
that contain both `SCHOBJ` and `UTAB`. The object-id extraction is still
heuristic and noisy; the next step is to decode the SYSOBJECTS row layout rather
than rely on nearby integer guesses.

Further validation showed object-id-like values for user tables appear more
stably in name-index-like contexts that contain `SCHOBJ` but not always `UTAB`:

| Object | Known online object id | Offline nearby candidate |
| --- | ---: | ---: |
| `DMDUL_MANY` | 33629 | 33629 |
| `DMDUL_ONE2` | 33630 | 33630 |
| `DMDUL_MOD2` | 33634 | 33634 |

The CLI now prints:

- `preferred_object_ids`: nearby values in the currently useful user-object id
  range;
- `likely_object_ids`: nearby plausible values sorted by distance to the name;
- `object_ids`: broader nearby integer candidates.

This is still a bootstrap heuristic. It is good enough to continue exploring
`SYSCOLUMNS` by object id, but it is not yet a full SYSOBJECTS decoder.

## SYSCOLUMNS Offline Column Definition Observations

Searching `SYSTEM.DBF` for a little-endian user table object id found compact
`SYSCOLUMNS`-like records around page 3059. For `DMDUL_MANY` object id `33629`,
the observed file offsets were:

| Column | Object id offset | Page | Page offset | Observed fields |
| --- | ---: | ---: | ---: | --- |
| `ID` | `25062974` | 3059 | 3646 | `colid=0`, `length=4`, `name=ID`, `type=INT` |
| `MARKER` | `25063022` | 3059 | 3694 | `colid=1`, `length=64`, `name=MARKER`, `type=VARCHAR` |
| `PAD` | `25063078` | 3059 | 3750 | `colid=2`, `length=3000`, `name=PAD`, `type=VARCHAR` |

The currently observed narrow pattern is:

```text
object_id u32le
column_id u16le
declared_length u32le
... row-specific bytes ...
prefixed column_name
prefixed type_name
```

The offline scanner therefore searches for the object id bytes and keeps the
nearest prefixed `column_name/type_name` pair after that object id. The scanner
reports file offset, page number, page offset, column id, declared length, name,
type, and a confidence score.

Remote validation against online `DBA_TAB_COLUMNS` matched all controlled
objects:

| Object id | Table | Offline columns |
| ---: | --- | --- |
| 33629 | `DMDUL_MANY` | `ID INT(4)`, `MARKER VARCHAR(64)`, `PAD VARCHAR(3000)` |
| 33630 | `DMDUL_ONE2` | `ID INT(4)` |
| 33631 | `DMDUL_NULL2` | `ID INT(4)`, `A INT(4)`, `B VARCHAR(20)`, `C BIGINT(8)`, `D VARCHAR(20)` |
| 33632 | `DMDUL_VLEN2` | `ID INT(4)`, `V VARCHAR(3000)` |
| 33633 | `DMDUL_DTTM2` | `ID INT(4)`, `D DATE(3)`, `T TIME(5)`, `TS TIMESTAMP(8)` |
| 33634 | `DMDUL_MOD2` | `ID INT(4)`, `V VARCHAR(40)` |

One important calibration point: the offline `column_id` observed in the raw
bytes is zero-based for these samples, while `DBA_TAB_COLUMNS.COLUMN_ID` is
one-based. The extractor should normalize this when presenting metadata.

The following fields remain unresolved:

- exact `SYSCOLUMNS` row boundaries;
- scale/precision encoding for numeric types;
- nullable flag and default value layout;
- whether the observed zero-based column id holds for all table forms.

### Online/Offline SYSCOLUMNS Calibration

Because the test database is available, dictionary table layout should be
calibrated by comparing online SQL results with the copied `SYSTEM.DBF`, not by
raw byte guessing alone.

The online definition of `SYS.SYSCOLUMNS` is:

| COLID | Name | Type | Length | Scale | Nullable |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | `NAME` | `VARCHAR` | 128 | 0 | N |
| 1 | `ID` | `INT` | 4 | 0 | N |
| 2 | `COLID` | `SMALLINT` | 2 | 0 | N |
| 3 | `TYPE$` | `VARCHAR` | 128 | 0 | N |
| 4 | `LENGTH$` | `INT` | 4 | 0 | N |
| 5 | `SCALE` | `SMALLINT` | 2 | 0 | N |
| 6 | `NULLABLE$` | `CHAR` | 1 | 0 | N |
| 7 | `DEFVAL` | `VARCHAR` | 2048 | 0 | Y |
| 8 | `INFO1` | `SMALLINT` | 2 | 0 | Y |
| 9 | `INFO2` | `SMALLINT` | 2 | 0 | Y |

For `DMDUL_MOD2`, online SQL reports object id `33634` and two column rows:

| COLID | NAME | TYPE$ | LENGTH$ | SCALE | NULLABLE$ |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | `ID` | `INT` | 4 | 0 | Y |
| 1 | `V` | `VARCHAR` | 40 | 0 | Y |

The corresponding offline scanner found:

```text
SYS.SYSCOLUMNS object_id=33634
page 3059 offset 4395: COLID 0, NAME ID, TYPE$ INT, LENGTH$ 4
page 3059 offset 4443: COLID 1, NAME V, TYPE$ VARCHAR, LENGTH$ 40
```

Raw page 3059 rows show the fixed fields are already aligned when using the
online definition. Example row for `NAME='ID', TYPE$='INT'`:

```text
0030 00000c 62830000 0000 04000000 0000 59 00000000
82 4944 83 494e54 ac1500000000ffffffff7fffff30d734040000
```

Working split:

| Relative offset | Bytes | Meaning |
| ---: | --- | --- |
| 0 | `0030` | row length/status |
| 2 | `00000c` | row metadata/control, not decoded |
| 5 | `62830000` | `ID=33634` |
| 9 | `0000` | `COLID=0` |
| 11 | `04000000` | `LENGTH$=4` |
| 15 | `0000` | `SCALE=0` |
| 17 | `59` | `NULLABLE$='Y'` |
| 18 | `00000000` | nullable/control bytes for `DEFVAL`, `INFO1`, and `INFO2`, not decoded |
| 22 | `82 4944` | `NAME='ID'` |
| 25 | `83 494e54` | `TYPE$='INT'` |
| 29 | `ac15...` | row tail/control begins after variable values |

This does not prove a dictionary-specific row format. It proves the previous
generic row model is incomplete for nullable columns in general: `ceil(column_count/4)`
is not sufficient to locate variable fields when nullable columns are present.
The same issue is visible in controlled nullable user-table rows such as
`DMDUL_NULL2`. The current decoder now uses the calibrated NULL bitmap rule for
ordinary rows: fixed-width NULL fields still consume their fixed bytes, while
variable-width NULL fields do not consume a variable prefix or payload.

The practical result is positive: for clean `SYSCOLUMNS` rows, the online values
and offline bytes are close enough to implement a real dictionary row decoder
without UNDO support. Remaining blockers are exact dictionary-row boundary
coverage and unsupported row metadata states, not the base field storage and not
a separate dictionary-table storage format.

The row-aware `SYS.SYSCOLUMNS` scanner now prefers this calibrated clean-row
layout before falling back to the older nearby-string heuristic. A trial
bootstrap against the checkpointed `SYSTEM.DBF` plus `DMDUL_TS01.DBF` produced:

```bash
PYTHONPATH=src python3 -m dmdul.cli bootstrap-dicts /tmp/dmdul_dbcopy \
  --output-dir /tmp/dmdul_dict_try \
  --table DMDUL_MOD2 \
  --table DMDUL_TYPE_STORE \
  --table DMDUL_NUM38_STORE \
  --experimental-heuristic-dicts
```

Output row counts:

| File | Rows |
| --- | ---: |
| `file.dict` | 2 |
| `user.dict` | 1 |
| `tab.dict` | 3 |
| `col.dict` | 24 |

The extracted column definitions for the expanded type table are now aligned
with online SQL, including type names that previously confused the string
heuristic:

```text
C_TINY  TINYINT   length 1
C_SMALL SMALLINT  length 2
C_INT   INT       length 4
C_BIG   BIGINT    length 8
C_NUM   NUMBER    length 30 scale preserved in col.dict/DDL when decoded
C_DEC   DECIMAL   length 18 scale preserved in col.dict/DDL when decoded
C_FLOAT FLOAT     length 8
C_DATE  DATE      length 3
C_TIME  TIME      length 5
C_TS    TIMESTAMP length 8
C_CLOB  CLOB      length 2147483647
C_BLOB  BLOB      length 2147483647
```

Current limitation: resolving the system dictionary tables themselves through
the same target-table resolver still fails for `SYSOBJECTS`, `SYSCOLUMNS`, and
`SYSINDEXES` because their object ids are `0`, `1`, and `2`; scanning for those
low ids is too noisy and needs a dedicated system-root/bootstrap path instead of
the user-table object-id path.

## SYSINDEXES Offline Storage Root Observations

For ordinary table storage, `SYS.SYSOBJECTS` contains an internal child object
named `INDEX<storage_index_id>`:

| Name | ID | TYPE$ | SUBTYPE$ | PID |
| --- | ---: | --- | --- | ---: |
| `INDEX33595349` | 33595349 | `TABOBJ` | `INDEX` | 33629 |
| `INDEX33595350` | 33595350 | `TABOBJ` | `INDEX` | 33630 |
| `INDEX33595351` | 33595351 | `TABOBJ` | `INDEX` | 33631 |
| `INDEX33595352` | 33595352 | `TABOBJ` | `INDEX` | 33632 |
| `INDEX33595353` | 33595353 | `TABOBJ` | `INDEX` | 33633 |
| `INDEX33595354` | 33595354 | `TABOBJ` | `INDEX` | 33634 |

`PID` points back to the owning table object id. This is the offline bridge
needed before probing `SYSINDEXES.ID`.

The `find-sysobject-indexes` scanner now recovers this bridge directly from
`SYSTEM.DBF` by anchoring on the parent table object id and selecting nearby
prefixed `TABOBJ` plus `INDEX<digits>` strings:

```sh
bic-dmdul find-sysobject-indexes SYSTEM.DBF 33630
```

Remote validation showed the highest-scoring candidate for each controlled
table object id is the expected storage index id:

| Table object id | Expected storage index id | Highest-scoring offline candidate | Page | Page offset |
| ---: | ---: | ---: | ---: | ---: |
| 33629 | 33595349 | 33595349 | 5697 | 3305 |
| 33630 | 33595350 | 33595350 | 5697 | 3354 |
| 33631 | 33595351 | 33595351 | 5697 | 3403 |
| 33632 | 33595352 | 33595352 | 5697 | 3452 |
| 33633 | 33595353 | 33595353 | 5697 | 3501 |
| 33634 | 33595354 | 33595354 | 5697 | 3550 |

Because these SYSOBJECTS index rows are packed consecutively, lower-scoring
neighboring `INDEX<id>` names may appear in the same scan window. The current
rule is to use the highest-scoring candidate, where the correct candidate is
nearest after the parent object id and has matching little-endian index id bytes
nearby. The exact `PID`, `TYPE$`, and `SUBTYPE$` byte offsets still need a full
row-layout decoder.

Searching `SYSTEM.DBF` for these storage index ids found compact
`SYSINDEXES`-like rows on page 5642:

| Index id | File offset | Page offset | Unique | Group | Root file | Root page | Type | Flag |
| ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | ---: |
| 33595349 | 46225383 | 6119 | `N` | 6 | 0 | 80 | `BT` | 1 |
| 33595350 | 46225440 | 6176 | `N` | 6 | 0 | 144 | `BT` | 1 |
| 33595351 | 46225497 | 6233 | `N` | 6 | 0 | 160 | `BT` | 1 |
| 33595352 | 46225554 | 6290 | `N` | 6 | 0 | 176 | `BT` | 1 |
| 33595353 | 46225611 | 6347 | `N` | 6 | 0 | 192 | `BT` | 1 |
| 33595354 | 46225668 | 6404 | `N` | 6 | 0 | 208 | `BT` | 1 |

The narrow observed field layout is:

```text
index_id u32le
is_unique char(1)          -- 'N' or 'Y'
group_id u16le
root_file u16le
root_page u32le
type_name char(2)          -- 'BT' in tested ordinary table storage
xtype u32le
flag u32le
...
```

The new `find-sysindex` scanner uses this layout to recover the storage root
directly from `SYSTEM.DBF`:

```sh
bic-dmdul find-sysindex SYSTEM.DBF 33595350
```

This completed the first offline recovery path for `SYSINDEXES.ID ->
GROUPID/ROOTFILE/ROOTPAGE`. Combined with the child-index scanner above, the
current heuristic path can map `table object id -> storage index id ->
GROUPID/ROOTFILE/ROOTPAGE` without online views for the controlled ordinary
tables.

## Offline Extraction Bootstrap

The first practical offline metadata path should be:

1. Read data file page size and group id from file header/control pages.
2. Identify `SYSTEM.DBF` and parse its early metadata.
3. Locate or scan for the SYS dictionary BTREE roots:
   - `SYSOBJECTS`
   - `SYSCOLUMNS`
   - `SYSINDEXES`
4. Decode those tables enough to recover:
   - object id to name/schema/type
   - column definitions by object id
   - storage roots from `SYSINDEXES`
5. Use `GROUPID/ROOTFILE/ROOTPAGE` to open user table BTREEs.
6. Traverse or scan leaf pages and decode rows using recovered column metadata.

Until the dictionary bootstrap is reliable, a transitional extractor mode may
accept dictionary metadata exported from a healthy database. That mode is useful
for row/page decoder development but is not sufficient for DUL-style recovery.

### Remote End-To-End CSV Validation

The current `extract-csv --database-dir` path has been validated on the remote
DM8 test files without querying online dictionary views:

```sh
cd /tmp/dmdul_run
PYTHONPATH=src /opt/gbase/python3.8/bin/python3.8 -m dmdul.cli \
  extract-csv \
  --database-dir /dmdata/data/DAMENG \
  --table DMDUL_MANY \
  --output /tmp/dmdul_many.csv \
  --scan-pages 64 \
  --report-output /tmp/dmdul_many_report.json
```

Result:

```text
table=SYSDBA.DMDUL_MANY
rows_written=80
rows_skipped_deleted=0
rows_skipped_decode_error=0
ok=true
mode=calibrated-metadata-page-range-scan
```

CSV verification showed:

- `81` CSV lines including the header;
- header `ID,MARKER,PAD`;
- `80` data rows;
- IDs cover `1..80` with no missing values and no duplicates;
- `PAD` values decode to length `3000`;
- accepted page refs were the 40 leaf pages `96..135`.

The earlier run reported warning diagnostics:

- `segment-root-candidate-ref-non-data-page`: segment-root candidate reference
  scanning is still broad and reports non-data references;
- `page-plan-btree-internal-descent`: the extractor used the BTREE root/internal
  page to reach leaf pages.

The previous `segment-manifest-data-file-without-control-entry` warning for
`DMDUL_TS01.DBF` was traced to an implementation bug: the same `sample_limit`
used for printable evidence display also capped DBF path hint scanning in
`dm.ctl`, so later control-file entries were missed. Control-file DBF hint
scanning is now decoupled from display sampling. On the remote file set,
`DMDUL_TS01.DBF` matches `dm.ctl` and control-backup entries by basename and
normalized path evidence, and `DMDUL_NULL2 --strict` returns `strict_ok=true`.

The segment-root non-data-reference warning is intentionally retained as
exploration evidence, but it is not treated as a strict extraction failure when
the accepted page plan is otherwise valid. It comes from a broad sliding-window
candidate scan over root bytes, not from the final set of pages decoded into the
CSV.

This proved the first practical offline chain for controlled ordinary tables:
SYSTEM dictionary scans -> table object id -> column metadata -> child storage
index -> `SYSINDEXES` root -> data pages -> decoded CSV. Later work added
fuller scalar coverage, inline/out-of-line LOB following for the verified
locator shape, row archive output, and row-archive import validation. It still
does not prove complete MVCC visibility, chained/overflow rows, encrypted
objects, ASM, or arbitrary production file sets.

Additional remote validation compared offline CSV output with online `SELECT`
for controlled fixtures:

| Table | Offline result | Online comparison |
| --- | --- | --- |
| `DMDUL_ONE2` | 4 rows | matched integer boundary IDs `-2147483648`, `-1`, `1`, `2147483647` |
| `DMDUL_NULL2` | 4 rows, 0 decode errors | matched NULL combinations; NULL fields emitted as empty CSV fields |
| `DMDUL_MOD2` | 2 rows, 1 deleted row skipped | matched visible rows `MOD_KEEP_1` and `MOD_UPDATE_3_AFTER` |
| `DMDUL_VLEN2` | 8 rows | matched IDs and VARCHAR lengths `1,2,10,127,128,255,256,1000` |
| `DMDUL_DTTM2` | 3 rows | matched DATE/TIME/TIMESTAMP values; offline output normalizes zero padding and microseconds |
| `DMDUL_EXT2` | 25,000 rows, strict mode OK | multi-extent table; IDs `1..25000`, `sum(ID)=312512500`, bucket/marker/pad formulas all matched |

The bootstrap dictionary path now preserves `SYSCOLUMNS` scale and nullable
fields in `col.dict`. A remote `bootstrap` run wrote `file.dict`/`user.dict`/
`tab.dict`/`col.dict` under
`/home/dmdba/dmdul/tmp/bootstrap_scale_nullable`; `DMDUL_TYPES3` produced 15
column rows with `scale=4` for `NUMBER(18,4)`/`DECIMAL(18,4)` and `scale=6` for
`TIME(6)`/`TIMESTAMP(6)`. Running `dump-data` from those dictionary files wrote
4 rows with `strict_ok=true`, and the generated DUL header now renders
`C_NUMBER NUMBER(18,4)`, `C_DECIMAL DECIMAL(18,4)`, `C_TIME TIME(6)`, and
`C_TS TIMESTAMP(6)`.

The same run also showed an owner/schema bug: the heuristic schema mapping
emitted `TEST2.DMDUL_TYPES3` for this object. Online calibration showed the
actual `SYS.SYSOBJECTS` row has `SCHID=150994945` (`0x09000001`), while the
old scanner had truncated the value to one byte. Reading `SCHID` as a 4-byte
value and seeding verified built-in schema ids fixes this controlled case:
remote `bootstrap_owner_fix` produced `owner=SYSDBA`,
`qualified_name=SYSDBA.DMDUL_TYPES3`, `schema_id=150994945`, and
`dump-data --table SYSDBA.DMDUL_TYPES3` wrote 4 rows with `strict_ok=true`. At
that point arbitrary user schema rows and duplicate table names still required
more schema/user dictionary decoding.

Follow-up raw-byte calibration found the ordinary `SCH` schema-row pattern. In
clean rows the type/name sequence is `83 SCH <name>`, and the full schema id is
stored as a little-endian u32 eight bytes before the prefixed `SCH` marker.
Examples matched online `SYS.SYSOBJECTS`: `KYD=150995945` (`e9 03 00 09`),
`KYD2=150995946`, `TEST=150995949`, and `SYSJOB=150995950`. Bootstrap now uses
that layout and filters schema ids to the observed `0x09xxxxxx` range, removing
false owner rows such as ASCII-like high integers while preserving real ordinary
schemas.

A duplicate-schema validation then created two same-name tables in `DMDUL_TS`:
`SYSDBA.DMDUL_DUP_SCHEMA2` and `TEST.DMDUL_DUP_SCHEMA2`. The first targeted
bootstrap attempt exposed that the old resolver ignored owner during name-based
resolution and mapped both owners to object `34019`. The resolver now selects
`SCHOBJ/UTAB` rows by owner-derived full schema id. The corrected targeted
bootstrap produced distinct entries: `SYSDBA` object `34019`, storage
`33595830`, root page `1280`; `TEST` object `34020`, storage `33595831`, root
page `1296`. `dump-data` wrote both DUL files with `strict_ok=true` and two rows
each, preserving owner-specific marker values. This proves duplicate table names
can be separated for the observed ordinary schema/table row layouts. The first
owner-aware targeted resolver was correct but inefficient because it rescanned
`SYSTEM.DBF` per requested table. The first fix preloaded decoded `SYSOBJECTS`
rows once in memory and reused them for every requested table. The second reuse
path is `bootstrap --source-dict-dir`: remote
`bootstrap_dup_schema_20260702_v5_from_dict` filtered the same two duplicate
tables from an existing dict directory in about 1 second, with no SYS dictionary
file read, and `dump-data` extracted both tables successfully from the filtered
dictionary set.

The current lower-level bootstrap path no longer scans the whole `SYSTEM.DBF`
for the core dictionaries. DM7 and DM8 comparison found a bootstrap-like
structure in SYSTEM.DBF page 0: offset `0x80` stores the `SYSOBJECTS` root page
and offset `0x7c` stores the `SYSINDEXES` root page. The root page header then
supplies the storage id. After that, the extractor verifies page-header
ownership, walks the BTREE/root leaf chain, and decodes live slot rows.
`SYSCOLUMNS` is reached through the offline-decoded `SYSINDEXES` row rather
than a string scan:

| Dictionary | Storage id | Root file | Root page | Remote result |
| --- | ---: | ---: | ---: | --- |
| `SYSOBJECTS` | `33554540` | `0` | `16` | 760 pages, 4,828 rows seen, 1,583 rows decoded |
| `SYSINDEXES` | `33554434` | `0` | `288` | 303 pages, 2,465 rows seen, 2,447 rows decoded |
| `SYSCOLUMNS` | `33554433` | `0` | `80` | 70 pages, 5,206 rows decoded, 0 failures |

Remote `bootstrap_full_storage_20260702_v3` completed in about 3.3 seconds and
generated `user=10`, `tab=1573`, `col=5206`. Remote
`bootstrap_root_discovery_20260703` then discovered `SYSOBJECTS` root page `16`
and `SYSINDEXES` root page `288` from page headers. Remote
`bootstrap_file_header_20260703` moved one layer lower: it read those two root
pages directly from SYSTEM page 0, decoded storage ids `33554540` and
`33554434` from the root page headers, decoded `SYSCOLUMNS` root page `80` from
offline `SYSINDEXES`, and generated `user=10`, `tab=1574`, `col=5206`. The same
page 0 offsets were validated against the parallel DM7 database at
`/dmdata/data7/DAMENG`; full DM7 bootstrap completed with `user=6`, `tab=344`,
`col=4739` and one `SYSCOLUMNS` row-decode failure sample. The resulting DM8
dictionary preserved owner/root metadata for controlled tables including
`TEST.DEPARTMENTS`, `SYSDBA.DMDUL_DUP_SCHEMA2`, `TEST.DMDUL_DUP_SCHEMA2`,
`SYSDBA.DMDUL_EXT2`, `SYSDBA.DMDUL_MANY`, and `SYSDBA.DMDUL_TYPES3`.
`dump-data --dict-dir` from that storage-root dictionary then extracted both
duplicate-schema tables and `SYSDBA.DMDUL_TYPES3` with `strict_ok=true`. The
remaining bootstrap work is to characterize more SYSTEM page 0 fields and test
additional DM builds, not to rely on online dictionary views.

The current strict batch comparison writes remote evidence under
`/home/dmdba/dmdul/tmp/strict_compare_current`. The generated
`strict_online_compare.json` recorded all seven controlled tables as matched:
`DMDUL_MANY`, `DMDUL_ONE2`, `DMDUL_NULL2`, `DMDUL_VLEN2`, `DMDUL_DTTM2`,
`DMDUL_MOD2`, and `DMDUL_EXT2`. The comparison uses full row-set equality for
the small tables, normalized temporal display forms for `DMDUL_DTTM2`, ID and
payload-length checks for the 3000-byte `DMDUL_MANY` rows, and aggregate plus
per-row formula verification for `DMDUL_EXT2`.

`DMDUL_EXT2` was created in `DMDUL_TS` specifically to exceed a single extent.
Online `DBA_SEGMENTS` reported `BLOCKS=880` and `EXTENTS=55`. The current
`DBA_EXTENTS` view on this DM8 instance returned only one row for this segment
(`EXTENT_ID=24`, `BLOCKS=4`), so it should not currently be treated as an
Oracle-compatible per-extent map without more decoding. The extractor scanned
862 accepted data page references for `DMDUL_EXT2` and wrote 25,000 rows with
`strict_ok=true`.

The `DMDUL_EXT2` BTREE also refined root-entry coverage checks. Root page `384`
uses leftmost child page `386`, which descends to leaf page `400`; its root slot
entry points to page `387`, another internal page. Page `387` does not expose
the same leftmost-child field, but its slot entries point to leaf pages already
covered by the `400..1261` leaf next-chain. Therefore a root entry child should
not be treated as uncovered merely because the child page itself is not in the
leaf chain. The planner now considers an internal child covered when its
leftmost descendant leaf or all of its entry children are covered by the walked
leaf chain. After that refinement, `DMDUL_EXT2 --strict` still writes 25,000
verified rows and no longer reports `page-plan-btree-root-entry-mismatch`.
The extraction report mode is now derived from the actual page planner; for this
case it reports `mode=btree-internal-descent`, matching the diagnostic evidence
instead of the older generic range-scan label. If
`page-plan-btree-root-entry-mismatch` appears in a future run, strict mode now
fails because an uncovered root child can mean an incomplete CSV.

## Next Experiments

- Create one-column tables to isolate row header and NULL bitmap layout.
- Create tables with only `DATE`, only `TIME`, and only `TIMESTAMP` values.
- Insert `VARCHAR` lengths around thresholds: 0, 1, 2, 10, 127, 128, 255, 256,
  3000.
- Use deletes and updates to identify row deletion flags and free space reuse.
- Force more BTREE levels to distinguish root, internal, and leaf page types.
- Explore file header fields by comparing multiple tablespaces with different
  sizes and file numbers.
- Determine how `SYS.V$DATAFILE` metadata is persisted on disk.
## Segment Root / BTree Root Exploration - 2026-07-01

This section records the current answer to the question: does a table's storage
root page have bitmap pages after it, and can we parse the segment header enough
to create an exact page plan?

### Current Evidence

Controlled table evidence from `evidence/type_store/DMDUL_TS01.DBF` shows two
patterns.

Small tables whose rows fit in one page usually have:

```text
root page     kind 0x14        BTREE data page
root + 1      kind 0x1a1a001a  small internal metadata/anchor page
root + 2...   kind 0xffff00ff  initialized empty pages
```

Examples: `DMDUL_ONE2`, `DMDUL_DTTM2`, `DMDUL_NUM38_STORE`,
`DMDUL_TYPE_STORE`. For these tables, `root+1` is not a useful bitmap in the
current evidence: it contains only header/anchor fields and a few tail bytes; the
rest is zero.

The larger table `TEST2.DMDUL_MANY` has a different root page:

```text
root page 80      kind 0x15        BTREE root/internal page
root + 1 page 81  kind 0x1a1a001a  metadata/anchor page
pages 82..95      kind 0xffff00ff  initialized empty pages
data pages 96..135 kind 0x14       BTREE leaf/data pages, linked by prev/next
```

The data pages are not discovered from a bitmap after the root. They are
discovered from the BTREE root/internal page itself.

### DMDUL_MANY Root Page 80

Important page 80 header fields observed:

```text
page kind                 0x15
storage id                33595349
entry count field          39
leftmost child candidate   96   (offset 0x52 as u16)
```

The data page chain found by scanning page headers is exactly:

```text
96 -> 97 -> 98 -> ... -> 135
```

Page 80 explains that same chain without a fallback range scan:

- offset `0x52` stores the leftmost child page: `96`;
- the root page has 39 slot entries at the page tail;
- each entry is currently observed as 15 bytes;
- each entry contains a child page number and a separator key.

Representative entries:

```text
entry off 3137: 00 0f 00 61 00 00 00 00 00 03 00 00 00 00 00
                child page 97, separator key 3

entry off   98: 00 0f 00 62 00 00 00 00 00 05 00 00 00 00 00
                child page 98, separator key 5

entry off 6176: 00 0f 00 63 00 00 00 00 00 07 00 00 00 00 00
                child page 99, separator key 7

...

entry off 6716: 00 0f 00 87 00 00 00 00 00 4f 00 00 00 00 00
                child page 135, separator key 79
```

This gives the exact child page list:

```text
[96] + [97, 98, 99, ..., 135]
```

This matches the actual BTREE leaf/data pages whose page headers have
`storage_id = 33595349` and whose `prev/next` chain runs from page 96 to 135.

### Root+1 Metadata Page

The `0x1a1a001a` page after each root is stable but does not currently look like
an extent bitmap. It has only a small number of nonzero fields. Current useful
fields include:

```text
offset 0x3a: storage id
offset 0x46: small-table value 1; DMDUL_MANY value 4; suspected allocation/extent count, unconfirmed
offset 0x4a: total row count in current evidence; examples:
             DMDUL_ONE2=4, DMDUL_DTTM2=3, DMDUL_TYPE_STORE=3, DMDUL_MANY=80
```

`offset 0x46` needs more samples before naming it. The current hypothesis is an
allocation-unit or extent-related count because `DMDUL_MANY` is the only sample
with value 4 while small one-page tables have value 1.

### Current Answer

No convincing extent bitmap has been found immediately after the table storage
root in the current controlled evidence. The page after root is a small
metadata/anchor page, not a populated bitmap.

For a table whose root page is itself a data page (`kind 0x14`), the root page is
the only data page in current small-table samples.

For a table whose root page is an internal/root page (`kind 0x15`), the root page
contains enough BTREE child-page entries to build an exact page plan. In the
`DMDUL_MANY` sample, parsing offset `0x52` plus the 39 child entries gives all
40 data pages precisely, avoiding the scan-range fallback.

### Implementation Direction

The next page-plan implementation should be:

1. If explicit page refs exist, use them.
2. Else read the root page.
3. If root kind is `0x14`, treat root as a data page and verify storage id.
4. If root kind is `0x15`, parse it as a BTREE internal/root page:
   - read leftmost child page from offset `0x52`;
   - read slot offsets from the page tail using the root entry count;
   - parse each observed 15-byte child entry to collect child page numbers;
   - verify each child page header has kind `0x14` and matching storage id;
   - follow/validate the leaf `prev/next` chain.
5. Keep storage-id scan as a fallback diagnostic only when root parsing is not
understood.

This is a stronger and more precise answer than looking for a bitmap in the
post-root pages.

### Table Storage BTree Versus Independent Index Segments

This evidence should not be interpreted as every table having a separate index
segment for its data. The observed `0x15` page is currently best described as a
BTREE root/internal page inside the table storage object: root page 80, child
pages 96..135, and all leaf pages share the same table `storage_id = 33595349`.

Production systems may also have independent primary-key or secondary-index
segments. Those must be identified through `SYSINDEXES` and their own storage
ids/root pages. The downloader must therefore keep these concepts separate:

- table storage object: the storage id used to recover table rows; it may be
  organized as a BTREE with root/internal/leaf pages;
- index storage object: a separate `SYSINDEXES` object with its own storage id,
  root page, and entries pointing to keys/rows.

For table data recovery, bic-dmdul should first parse the table storage object's
BTREE page plan. Index segments can be used later as auxiliary evidence or for
index export, but should not be conflated with the table storage id unless
`SYSINDEXES` proves that relationship.

Production extent allocation must also be treated as non-contiguous. Child pages
from a BTREE root and leaf `prev/next` links are safer than scanning a contiguous
range because other objects' extents can appear between a table's extents. Every
planned page must still be verified by page header identity and storage id.
## SYSOBJECTS Owner and Partition Findings - 2026-07-03

A later `SYSCOLUMNS.COLID=2560` failure was traced to an upstream
`SYSOBJECTS` parse error, not to the clean `SYSCOLUMNS` row formula. The target
table `SYSDBA.DMDUL_TYPE_COVER5` was incorrectly decoded as object id
`16777349`; online dictionary and slot-row evidence show the real object id is
`34097`. Looking up columns for the wrong object id mixed unrelated rows and
created the false non-contiguous column sequence.

The observed DM8 `SYSOBJECTS` ordinary slot layout is:

```text
row.data[7:11]   ID
row.data[11:15]  SCHID
row.data[15:19]  PID, 0xffffffff for no parent
variable area    NAME, TYPE$, SUBTYPE$
```

Object selection must use `(SCHID, NAME, PID)` plus decoded `TYPE$` and
`SUBTYPE$`; table name alone is not a valid key when owner/schema information is
available.

Partition evidence from `SYSDBA.DMDUL_PART_T`:

| Object | ID | TYPE$ | SUBTYPE$ | PID |
| --- | ---: | --- | --- | ---: |
| `DMDUL_PART_T` | 34099 | `SCHOBJ` | `UTAB` | -1 |
| `DMDUL_PART_T_P1` | 34100 | `SCHOBJ` | `UTAB` | 34099 |
| `DMDUL_PART_T_P2` | 34101 | `SCHOBJ` | `UTAB` | 34099 |
| `INDEX33595928` | 33595928 | `TABOBJ` | `INDEX` | 34099 |
| `INDEX33595929_33595928` | 33595929 | `TABOBJ` | `INDEX` | 34100 |
| `INDEX33595930_33595928` | 33595930 | `TABOBJ` | `INDEX` | 34101 |

The parent partitioned table and each partition are all `SCHOBJ/UTAB`; the
difference is `PID`. Extraction of a partitioned parent table must expand child
objects where `PID=<parent table id>` and read each partition's own
`TABOBJ/INDEX` storage object. Remote strict extraction of
`SYSDBA.DMDUL_PART_T` scanned partition root pages `1376` and `1392` and wrote
the expected rows `1,P1` and `101,P2`.

Complex partition follow-up used three controlled tables:

- `SYSDBA.DMDUL_PART_LIST`: list partitions `P_CN`, `P_US`, `P_OTHER`.
- `SYSDBA.DMDUL_PART_HASH`: four hash partitions `DMHASHPART0..3`.
- `SYSDBA.DMDUL_PART_RANGE_HASH`: range partitions `P_LOW/P_HIGH`, each with
  two hash subpartitions.

The dictionary pattern remains recursive:

```text
table parent:       SCHOBJ/UTAB, PID=-1
intermediate part:  SCHOBJ/UTAB, PID=<parent table or partition id>
leaf part/subpart:  SCHOBJ/UTAB, PID=<parent partition id>
data storage:       TABOBJ/INDEX, PID=<leaf object id>
```

For table-data extraction, bic-dmdul should ignore intermediate storage objects and
use only leaf partition/subpartition objects, defined as partition descendants
with no further `SCHOBJ/UTAB` children. Remote strict extraction validated:

| Table | Leaf data pages | Rows |
| --- | --- | ---: |
| `DMDUL_PART_LIST` | `1424`, `1440`, `1456` | 3 |
| `DMDUL_PART_HASH` | `1488`, `1504`, `1520`, `1536` | 4 |
| `DMDUL_PART_RANGE_HASH` | `1568`, `1584`, `1600`, `1616` | 4 |

The range-hash output rows matched the inserted values:
`RH_LOW1`, `RH_LOW2`, `RH_HIGH1`, and `RH_HIGH2`.

Bootstrap/dump split:

- `bootstrap` is responsible for scanning SYSTEM dictionary tables and writing
  table metadata to dict files in the normal path.
- If SYSTEM dictionaries are missing or unreadable, normal `bootstrap -b` must
  fail with error diagnostics instead of treating empty dictionaries as valid.
- `bootstrap --scan-storages-without-system-dicts` is a separate disaster
  recovery path. It scans all DBF page headers, groups `page_kind=0x14` pages by
  `(group_id, file_no, storage_id)`, writes `storage_scan.dict`, and creates
  `SCAN.TAB_<storage_id>` placeholders for raw-row export.
- `dump-data --dict-dir` must consume only `file.dict`, `tab.dict`, and
  `col.dict`; it must not rescan `SYSOBJECTS`, `SYSCOLUMNS`, or `SYSINDEXES`.
- `dump-data --scan-storage-dict` consumes the scan dictionary placeholders and
  writes `raw_row VARBINARY` DUL output. It does not infer owner, table name, or
  columns.
- For partitioned tables, `tab.dict.page_refs` stores all leaf partition root
  pages as `file_no:page_no` entries separated by semicolons, and
  `storage_index_ids` stores the corresponding leaf storage ids.
- When `page_refs` is present, the extractor treats these dict entries as the
  extraction plan and does not apply a single table-level storage-id filter.

Remote verification used one bootstrap pass for
`SYSDBA.DMDUL_PART_RANGE_HASH`. The generated table row contained:

```text
storage_index_ids=33595943;33595944;33595945;33595946
page_refs=0:1568;0:1584;0:1600;0:1616
```

The subsequent `dump-data --dict-dir tmp/bootstrap_complex_part_dict` wrote all
four rows from the already downloaded dict without rescanning SYSTEM.DBF.

## Current Format Summary

A concise Chinese summary of the currently verified file/page/row/type/LOB
formats is maintained in
`docs/DM8_STORAGE_FORMAT_SUMMARY_2026-07-03_CN.md`. Treat that document as the
quick implementation reference, and this architecture note as the longer
evidence log.

Latest implementation notes added after the original exploration:

- On-demand procedure DDL export reads `SYS.SYSTEXTS` only when
  `dump-procedures` runs. It supports inline and out-of-line CLOB source text.
- On-demand ordinary BTree index DDL export parses `SYSINDEXES.KEYNUM` and
  variable-length `KEYINFO`, using `col.dict` for column names.
- Row archive import now preserves legal temporal precision for
  `TIME/TIMESTAMP/DATETIME WITH TIME ZONE`; invalid dictionary scale values such
  as observed `4102` for `TIMESTAMP WITH LOCAL TIME ZONE` are not emitted as SQL
  precision.
- Remote end-to-end validation imported five row-archive exports into `DMTEST`.
  `DMDUL_MANY`, `BMSQL_DISTRICT`, `BMSQL_WAREHOUSE`, and
  `DMDUL_TIME_TYPES` matched source tables with bidirectional `MINUS=0/0`;
  `DMDUL_DUMP_TYPES` matched on non-LOB scalar columns.
