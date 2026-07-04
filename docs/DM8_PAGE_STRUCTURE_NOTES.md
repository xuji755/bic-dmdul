# DM8 Page Structure Notes

This note records the current byte-level understanding of DM8 data pages for
`dmdul`. It is evidence-driven and intentionally separates proven fields from
working candidates.

## Scope

Current evidence comes from:

- `evidence/system/SYSTEM.DBF`
- `evidence/type_store/DMDUL_TS01.DBF`
- controlled ordinary row-store BTREE tables in `DMDUL_TS`

The observed page size is `8192` bytes (`0x2000`). Most findings below are for
ordinary BTREE/data pages and dictionary BTREE/data pages.

## Page Header Fields

| Offset | Size | Endian | Working name | Meaning |
| ---: | ---: | --- | --- | --- |
| `0x00` | 4 | little | `group_raw` | Low 16 bits are group/tablespace id; high 16 bits are file-number hint. |
| `0x04` | 4 | little | `page_no` | Zero-based page number inside the file. |
| `0x08` | 6 | little parts | `prev_page_ref` | Previous page reference, or `ff ff ff ff ff ff` for null. |
| `0x0e` | 6 | little parts | `next_page_ref` | Next page reference, or `ff ff ff ff ff ff` for null. |
| `0x14` | 4 | little | `page_kind_raw` | Page role/class candidate. Stronger than first byte for page type. |
| `0x18` | 4 | little | checksum/hash candidate | Non-monotonic, page-specific value. Needs validation. |
| `0x1c` | 4 | little | page change/SCN candidate | Monotonic-looking in related BTREE pages. Needs checkpoint/DML validation. |
| `0x20` | 4 | little | control candidate | Often `5` in samples; exact meaning unknown. |
| `0x24` | 2 | little | count candidate | In BTREE/data samples often active row count plus control count. |
| `0x26` | 2 | little | row-area end/free-offset candidate | Tracks physical row-chain end or next free offset in samples. |
| `0x2c` | 2 | little | active slot count candidate | Current parser exposes this as observed row count. |
| `0x2e` | 2 | little | deleted/free row head candidate | Usually `ffff`; points to deleted row offset in delete sample. |
| `0x38` | 2 | little | storage prefix candidate | Root/header pages may differ from leaf pages; exact meaning unknown. |
| `0x3a` | 4 | little | `storage_id` | Matches `SYSINDEXES.ID` and `SYSOBJECTS` `TABOBJ/INDEX` child object id. |

The first page byte is not a reliable page type. It is the low byte of
`group_raw` in current evidence. For example, nonzero pages in `SYSTEM.DBF` have
first byte `0x00` because group id is `0`, while nonzero pages in
`DMDUL_TS01.DBF` have first byte `0x06` because group id is `6`.

## Address And Link Fields

`group_raw` is decoded as:

```text
group_id     = u32le(page[0x00:0x04]) & 0xffff
file_no_hint = u32le(page[0x00:0x04]) >> 16
```

A page reference is 6 bytes:

```text
u16le file_no + u32le page_no
```

Examples:

```text
ff ff ff ff ff ff -> null
00 00 60 00 00 00 -> file 0, page 96
00 00 61 00 00 00 -> file 0, page 97
```

## Page Kind Values

Observed `page_kind_raw` values at offset `0x14`:

| Value | Current label | Meaning |
| ---: | --- | --- |
| `0x00000013` | `tentative-file-control` | File/control page. |
| `0x00000011` | `tentative-space-bitmap` | Space-management or bitmap-like page. |
| `0x00000014` | `tentative-btree-data` | BTREE/data page. |
| `0x00000015` | `tentative-segment-root` | Segment root/header page candidate. |
| `0x1a1a001a` | `tentative-internal-metadata` | Internal/metadata companion page candidate. |
| `0xffff00ff` | `tentative-empty-initialized` | Empty initialized/free-like page. |
| `0x00000016` | unknown | Observed but not decoded. |
| `0x00000017` | unknown | Observed but not decoded. |
| `0x00000020` | unknown | Observed but not decoded. |
| `0x00000023` | unknown | Observed but not decoded. |
| `0x00000063/64/65` | unknown | Early metadata page candidates. |

## Storage Id And Page Ownership

The page header contains a storage object id at offset `0x3a`:

```text
storage_id = u32le(page[0x3a:0x3e])
```

This is not the parent table `SYSOBJECTS.ID`. It matches:

```text
SYSOBJECTS table-level child object:
  TYPE$='TABOBJ', SUBTYPE$='INDEX', ID=storage_id

SYSINDEXES:
  ID=storage_id
```

Example for `DMDUL_MANY`:

```text
SYSOBJECTS table object ID       = 33629
SYSOBJECTS TABOBJ/INDEX child ID = 33595349
SYSINDEXES.ID                    = 33595349
PAGE HEADER storage_id           = 33595349
```

Pages for the same table storage object carry the same `storage_id`:

```text
page 80  offset 0x38: 0100d59f0002 -> storage_id 33595349
page 96  offset 0x38: 0000d59f0002 -> storage_id 33595349
page 135 offset 0x38: 0000d59f0002 -> storage_id 33595349
```

The storage prefix at `0x38` is still a candidate. Root/header pages can have a
nonzero prefix where leaf/data pages have zero.

## Table Entry Chain

To locate a table's physical entry from offline dictionary files:

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
  GROUPID, ROOTFILE, ROOTPAGE = physical root/header page

PAGE HEADER
  u32le(page[0x3a:0x3e]) = storage_id, used to validate page ownership
```

`SYSOBJECTS` table object rows identify tables but do not currently show direct
`GROUPID/ROOTFILE/ROOTPAGE` values. Those physical entry fields come from
`SYSINDEXES`.

## Page Change Or SCN Candidate

The 8 bytes at `0x18` should be split into two `u32le` values:

```text
0x18-0x1b  checksum/hash/validation candidate
0x1c-0x1f  page change/SCN candidate
```

Example from linked user-table BTREE/data pages:

```text
page 96  raw@0x18=191f95dce377a5a4  lo32=dc951f19  hi32=a4a577e3
page 97  raw@0x18=cacbc0d5e677a5a4  lo32=d5c0cbca  hi32=a4a577e6
page 98  raw@0x18=e0582fc2e977a5a4  lo32=c22f58e0  hi32=a4a577e9
```

The high `u32` has a monotonic-looking sequence in related pages, while the low
`u32` looks hash-like. This still needs synchronized checkpoint and DML evidence
before being treated as a real SCN or LSN.

## Logical Page Diagram

```text
DM8 BTREE/DATA PAGE, 8192 bytes
+--------------------------------------------------------------------------------+
| PAGE HEADER / PAGE CONTROL                                                     |
|--------------------------------------------------------------------------------|
| 0x0000  4 bytes   group_raw                                                     |
| 0x0004  4 bytes   page_no                                                       |
| 0x0008  6 bytes   prev_page_ref                                                 |
| 0x000e  6 bytes   next_page_ref                                                 |
| 0x0014  4 bytes   page_kind_raw                                                 |
| 0x0018  4 bytes   checksum/hash candidate                                       |
| 0x001c  4 bytes   page change / SCN candidate                                   |
| 0x0020  ...       page control fields                                           |
| 0x0038  2 bytes   storage prefix candidate                                      |
| 0x003a  4 bytes   storage_id                                                    |
+--------------------------------------------------------------------------------+
| UNKNOWN / PAGE-SPECIFIC CONTROL AREA                                            |
|--------------------------------------------------------------------------------|
| Fields between PAGE HEADER and row area are not fully decoded yet.              |
| Current ordinary BTREE/data samples place row data at about 0x0062.             |
+--------------------------------------------------------------------------------+
| ROW DATA AREA                                                                   |
|--------------------------------------------------------------------------------|
| 0x0062  row 1                                                                   |
|         row length/status, row metadata, fixed area, variable area, tail/control |
|                                                                                |
|         row 2                                                                  |
|         row length/status, row metadata, fixed area, variable area, tail/control |
|                                                                                |
|         ...                                                                    |
|                                                                                |
|         Deleted/old physical rows may still be present here.                    |
+--------------------------------------------------------------------------------+
| FREE SPACE / UNDECODED GAP                                                      |
+--------------------------------------------------------------------------------+
| ROW SLOT DIRECTORY / ROW INDICATOR LIST                                         |
|--------------------------------------------------------------------------------|
| Grows from page tail backward. Observed entries are 2-byte little-endian         |
| offsets pointing to row heads in the row data area.                             |
+--------------------------------------------------------------------------------+
| PAGE TAIL / END                                                                 |
|--------------------------------------------------------------------------------|
| 0x2000  end of page                                                             |
+--------------------------------------------------------------------------------+
```

## Row Data

Observed physical row prefix:

```text
raw_len_flags = u16be(row[0:2])
length        = raw_len_flags & 0x7fff
deleted       = (raw_len_flags & 0x8000) != 0
```

Examples:

```text
00 25 -> live row, length 0x25
80 27 -> deleted row, length 0x27
```

Current clean non-NULL row layout:

```text
row length/status
row metadata/control bytes
fixed-width column area
variable-width column area
row tail/control
```

Variable-length values use:

```text
0..127 bytes:  one byte, 0x80 + length
>=128 bytes:   two-byte big-endian length
```

The row tail/control region is 19 bytes in current samples and likely carries
transaction, row-version, or undo-related state. It is not decoded enough for
visibility decisions.

## Row Slot Directory

Observed BTREE/data pages have 2-byte little-endian row offsets near the page
tail. These offsets point to active row heads.

Example page 224:

```text
row heads: 98, 158, 214, 286
slot bytes at 8174: 1e 01 d6 00 9e 00 62 00
decoded slots: 286, 214, 158, 98
```

Observed slot order is last physical row back to first physical row. Deleted
physical rows can remain in the row chain but are not listed as active slots.

## Missing SYSTEM.DBF Fallback

If `SYSTEM.DBF` is missing or damaged, pages can still be grouped by page-header
storage id:

```text
scan all DBF files
  -> read nonzero page headers
  -> group pages by u32le(page[0x3a:0x3e])
  -> classify root/header, internal, leaf/data, empty, and metadata pages
  -> follow same-storage next-page links where available
  -> emit storage_scan.dict and SCAN.TAB_<storage_id> physical recovery candidates
```

Current implementation exposes this path as
`bootstrap --scan-storages-without-system-dicts` plus
`dump-data --scan-storage-dict`. It can recover storage-object page sets and raw
rows, but not reliable schema names, table names, column names, exact column
types, or full MVCC visibility.

## Open Questions

- Confirm `0x1c` as SCN/LSN/page-change field using checkpoint and DML evidence.
- Confirm `0x18` as checksum/hash/validation field.
- Decode the `0x38` storage prefix.
- Decode root/internal page child pointers.
- Decode complete slot directory structure and state bits.
- Decode row metadata for NULL bitmap and column directory.
- Decode the 19-byte row tail/control region and its relation to MVCC/UNDO.
