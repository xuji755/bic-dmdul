# DM8 Storage Architecture Notes

This document records verified observations and working hypotheses for the
offline `dmdul` extractor.

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
multi-file tablespaces showed this was incomplete, and the first byte is now
also recorded independently as `page_type_raw` because DM PAGE type is commonly
stored at the beginning of the page:

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

## Segment And Extent Findings

`DBA_SEGMENTS` reports allocated segment blocks. For example:

| Segment | Type | Root/header block | `DBA_SEGMENTS.BLOCKS` |
| --- | --- | ---: | ---: |
| `DMDUL_T1` | table | 16 | 16 |
| `DMDUL_HEAP` | table | 48 | 16 |
| `DMDUL_TYPES` | table | 64 | 16 |
| `DMDUL_MANY` | table | 80 | 64 |

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

## Table Organization

Every ordinary user table observed has an associated `SYS.SYSINDEXES` BTREE-like
entry with:

- `TYPE$ = 'BT'`
- `ROOTFILE = 0`
- `ROOTPAGE = table root/header block`
- `GROUPID = tablespace id`

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
| observed row metadata | 1 byte for <=4 columns, 2 bytes for >=5 columns | must be all zero for supported non-NULL rows |
| user column payload | variable | decoded by column type |

Non-zero observed row metadata is rejected with `unsupported-row-metadata` until
the NULL bitmap, column directory, and transaction/MVCC flags are decoded.

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

NULL handling is not fully decoded yet. The row containing `C_NULL = NULL`
shows metadata bytes before the fixed values, but more controlled cases are
needed to distinguish row header, NULL bitmap, and column directory fields.

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
dmdul find-sysobject SYSTEM.DBF DMDUL_ONE2
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
dmdul find-sysobject-indexes SYSTEM.DBF 33630
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
dmdul find-sysindex SYSTEM.DBF 33595350
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
