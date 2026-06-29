# Exploration And Implementation Tasks

This checklist is ordered toward the final goal: offline table-to-CSV
extraction from intact DM8 data files.

## A. File And Tablespace Discovery

- [x] Confirm page size on the test instance: 8192 bytes.
- [x] Confirm logical storage hierarchy: database -> tablespace -> data file ->
  segment -> extent/cluster -> page.
- [x] Confirm extent/cluster default: 16 pages.
- [x] Observe file header/control pages in ordinary data files.
- [x] Decode file page 0 enough to identify:
  - group/tablespace id from low 16 bits of the first page-header field
  - file-number hint from high 16 bits of the first page-header field
  - total pages from file size and page size
- [ ] Decode file page 0 enough to identify:
  - page size or page-size code
  - file status/checkpoint fields if present
- [ ] Determine how `SYS.V$DATAFILE` metadata is persisted on disk.
- [x] Build local file discovery that scans candidate `.DBF` files and groups
  them by observed group id.

## B. Page Header And Space Management

- [x] Identify common page header fields:
  - group id
  - page number
  - previous page reference
  - next page reference
  - page kind candidate
- [x] Confirm 6-byte page references look like `u16 file_no + u32 page_no`.
- [ ] Decode page kind values:
  - file/control page
  - space bitmap page
  - BTREE/root page
  - BTREE/leaf data page
  - internal/metadata page
  - free/empty initialized page
- [ ] Decode page-level row count, free-space offset, slot directory offset, and
  object/storage id fields.
- [ ] Decode file or extent bitmap pages enough to distinguish allocated and
  free pages.

## C. Segment And BTREE Structure

- [x] Confirm ordinary tables have `SYS.SYSINDEXES` BTREE storage entries.
- [x] Confirm table storage entry uses `GROUPID`, `ROOTFILE`, `ROOTPAGE`.
- [x] Confirm small tables can store rows directly in the root page.
- [x] Confirm larger tables move rows to linked leaf pages.
- [ ] Decode root page child/leaf pointers.
- [ ] Traverse linked leaf pages without scanning the whole file.
- [ ] Decide fallback strategy: full segment scan when root traversal is not yet
  reliable.
- [ ] Decode how allocated extent lists are represented outside online views.

## D. SYS Dictionary Bootstrap

- [x] Identify first required dictionary tables:
  - `SYS.SYSOBJECTS`
  - `SYS.SYSCOLUMNS`
  - `SYS.SYSINDEXES`
- [x] Calibrate current physical roots:
  - `SYSOBJECTS`: SYSTEM page 16
  - `SYSCOLUMNS`: SYSTEM page 80
  - `SYSINDEXES`: SYSTEM page 288
- [x] Confirm dictionary rows use the same inline variable-length string format
  for object names and type names.
- [x] Add heuristic offline scanner that finds SYSOBJECTS-like candidates by
  object name directly in `SYSTEM.DBF`.
- [x] Add heuristic offline scanner that finds SYSCOLUMNS-like candidates by
  object id directly in `SYSTEM.DBF`.
- [x] Validate SYSCOLUMNS-like scanner against controlled objects
  `DMDUL_MANY`, `DMDUL_ONE2`, `DMDUL_NULL2`, `DMDUL_VLEN2`,
  `DMDUL_DTTM2`, and `DMDUL_MOD2`.
- [x] Add heuristic offline scanner that finds SYSINDEXES-like candidates by
  storage index id directly in `SYSTEM.DBF`.
- [x] Validate SYSINDEXES-like scanner against controlled storage indexes
  `33595349..33595354`.
- [x] Add heuristic offline scanner that maps a table object id to child
  `INDEX<storage_id>` SYSOBJECTS candidates.
- [x] Validate child-index scanner against controlled table object ids
  `33629..33634`.
- [ ] Decode enough `SYSOBJECTS` rows offline to recover object name/id/schema/type.
- [ ] Decode complete `SYSCOLUMNS` row layout offline, including scale,
  nullability, defaults, and exact column id base.
- [ ] Decode complete `SYSINDEXES` row layout offline, including `KEYINFO` and
  extent allocation fields.
- [ ] Decode complete `SYSOBJECTS` child-index row layout, including exact `PID`,
  `TYPE$`, and `SUBTYPE$` offsets.
- [ ] Remove hard-coded dictionary root pages by discovering them from file
  metadata or a reliable system bootstrap structure.

## E. Row Format And Type Decoding

- [x] Observe row length/status prefix.
- [x] Observe deleted-row high bit candidate in the row length/status prefix.
- [x] Implement physical row-chain scanner that can see deleted/updated row
  records beyond the page header's active-row count.
- [x] Observe fixed-width little-endian integer and double encodings.
- [x] Observe short `VARCHAR` length encoding: one byte `0x80 + len` for 0..127.
- [x] Observe long `VARCHAR` length encoding: two-byte big-endian length for
  128 and above.
- [ ] Decode row column directory, if present.
- [ ] Decode NULL bitmap/NULL column handling.
- [ ] Decode `CHAR` padding rules.
- [ ] Decode `DATE`.
- [ ] Decode `TIME`.
- [ ] Decode `TIMESTAMP`.
- [ ] Decode `DECIMAL/NUMBER`.
- [ ] Decode `FLOAT` and distinguish DM `FLOAT` from double storage.
- [ ] Decode update and delete row status flags precisely.

## F. CSV Extractor

- [x] Define internal metadata structures:
  - data files
  - table metadata
  - column metadata
  - storage root
- [x] Implement calibrated page-range scanner for ordinary BTREE data pages.
- [ ] Implement real BTREE leaf traversal from root/internal pages.
- [x] Implement row slicer from page body for observed ordinary BTREE data pages.
- [x] Implement row decoder for initial non-NULL `INT`, `BIGINT`, `VARCHAR`,
  and `CHAR` subset.
- [ ] Implement CSV writer with headers and proper escaping.
- [ ] Add CLI command:

```sh
dmdul extract-csv --database-dir ... --table OWNER.TABLE --output table.csv
```

- [x] Implement CSV writer with headers and proper escaping for the calibrated
  root-page scan path.
- [x] Add transitional CLI command that accepts calibrated JSON metadata.
- [x] Validate calibrated page-range row scan against controlled multi-page
  table `DMDUL_MANY` by decoding 80/80 rows from data files.
- [ ] Validate full CLI CSV output against online `SELECT *` for controlled
  test tables.

## G. Test Corpus

- [x] Create `DMDUL_T1`: mixed basic types, primary key.
- [x] Create `DMDUL_HEAP`: table without primary key.
- [x] Create `DMDUL_TYPES`: mixed scalar types.
- [x] Create `DMDUL_MANY`: multi-page table.
- [x] Create `DMDUL_ONE2`: single `INT` column.
- [x] Create `DMDUL_NULL2`: NULL pattern table.
- [x] Create `DMDUL_VLEN2`: `VARCHAR` length threshold table.
- [x] Create `DMDUL_DTTM2`: date/time/timestamp table.
- [x] Create `DMDUL_MOD2`: delete/update row-status table.
- [ ] Create one-column tables for each target type.
- [ ] Create rows around page capacity boundaries.
- [ ] Create deleted rows followed by insert reuse.
