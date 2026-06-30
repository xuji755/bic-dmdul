# Exploration And Implementation Tasks

This checklist is ordered toward the final goal: offline table-to-CSV
extraction from intact DM8 data files.

The current priority is foundational storage research, not adding more
best-effort extraction behavior. A task should be marked complete only when it
has controlled fixtures, raw-byte evidence, and parser/test coverage sufficient
to explain the underlying database structure.

## A. File And Tablespace Discovery

- [x] Confirm page size on the test instance: 8192 bytes.
- [x] Confirm logical storage hierarchy: database -> tablespace -> data file ->
  segment -> extent/cluster -> page.
- [x] Confirm extent/cluster default: 16 pages.
- [x] Treat `dm.ctl`/control files as first-class offline evidence because they
  store database file and tablespace structure when the instance cannot start.
- [x] Discover `dm.ctl`/`*.ctl` files and record conservative control-file
  evidence, including SHA-256 and DBF path hints.
- [x] Add standalone `dm.ctl` summary output for control-file-only evidence
  capture.
- [x] Add byte-level `dm.ctl` snapshot comparison output for controlled
  create/resize/add/drop data-file experiments.
- [x] Add phased SQL fixture for `dm.ctl` tablespace/data-file layout
  experiments.
- [ ] Decode the `dm.ctl` binary layout for:
  - database identity
  - tablespace entries
  - data-file entries
  - file numbers and paths
  - status/checkpoint fields
- [x] Observe file header/control pages in ordinary data files.
- [x] Record the first page byte separately as `page_type_raw` because DM page
  type is commonly stored at the beginning of the page.
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
- [ ] Identify database identity fields that prove all input files belong to the
  same database.
- [ ] Identify checkpoint/SCN/LSN fields in file headers and decide how to
  detect mixed or unsafe file copies.
- [ ] Determine which rollback/undo files are required for MVCC visibility.
- [ ] Detect missing files, sparse holes, short reads, and file-size/page-count
  mismatches before extraction.
- [x] Add database directory summary for discovered DBF files, group ids,
  file-number hints, SYSTEM candidates, duplicate hints, and sampled page kinds.
- [x] Add basic file-set diagnostics for trailing bytes, abnormal page0 page
  numbers, duplicate group/file hints, and sampled page-number mismatches.
- [x] Report short or otherwise unparsed `.DBF` files in database summaries
  instead of silently ignoring them.
- [x] Surface sampled same-file page-reference range diagnostics in database
  summaries.
- [x] Cross-check DBF path hints from control files against copied DBF basenames
  in database summaries.
- [x] Emit a `dm.ctl`-derived data-file manifest that attaches matched DBF
  page-0 group/tablespace id and file-number hints.
- [x] Preserve every observed `dm.ctl` DBF path occurrence with ordinal, offset,
  normalized path, and basename for offline data-file manifest reconstruction.
- [x] Emit stable summary-level diagnostic codes for missing control files,
  duplicate file hints, and control-file DBF hints not found in the copy.
- [x] Add conservative `preflight-database` gate that exits nonzero on current
  fatal file-set diagnostics.
- [x] Run conservative file-set preflight by default for
  `extract-csv --database-dir`.
- [x] Allow `extract-csv --database-dir` to persist its preflight JSON with
  `--preflight-output`.
- [x] Add a `bootstrap-dicts` command that materializes the bootstrap artifact
  set (`file.dict`, `user.dict`, `tab.dict`, `col.dict`) and writes a manifest.
- [x] Generate extraction-time `control.ctl` rows in the format
  `tablespace_id,file_id,full_local_path`, using local copied DBF paths and DBF
  page-0 headers so the file can also be hand-written when `dm.ctl` is absent.
- [x] Allow `bootstrap-dicts --table` to populate `user.dict`, `tab.dict`, and
  `col.dict` for a requested table using current SYSTEM.DBF heuristic scans.
- [ ] Replace target-table heuristic bootstrap with full SYSTEM dictionary
  extraction:
  - locate USER/TABLE/COLUMN dictionary table segments from SYSTEM metadata
  - scan those dictionary table segments without a target table name
  - write complete `user.dict`, `tab.dict`, and `col.dict`
  - include table object id, storage index id, tablespace id, file id, root page,
    column id, type id/name, precision, scale, length, nullable, and ordering

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
- [ ] Decode table/object identity fields from data page headers, including the
  field that proves a page belongs to a target table object or segment.
- [ ] Decode transaction and visibility fields from data page headers, including
  page-level change counters, transaction references, and rollback/undo pointers
  if present.
- [ ] Confirm or reject an Oracle-style block-level ITL/transaction-slot array:
  - compare page header and tail bytes before DML, during uncommitted DML, after
    rollback, and after commit plus checkpoint
  - compare row-head status bytes for committed and uncommitted insert, delete,
    and update rows
  - distinguish row-offset slot entries from any transaction-slot entries
  - prove whether transaction state is block-level, row-level, undo-level, or a
    combination of those
- [x] Add `analyze-block` exploration output that can be run on dictionary-table
  pages and ordinary-table pages to report page type candidates, exact
  `OBJECT_ID` byte matches, row chain entries, row layout metadata, and
  per-column storage traces.
- [ ] Decode page-level row count, free-space offset, slot directory offset, and
  object/storage id fields.
- [x] Add unknown-structure dump output for anonymous page-header bytes, row
  tail/control bytes, page-tail slot bytes, and 8/16/24-byte candidate chunks.
- [ ] Validate candidate page-header fields from unknown-structure dumps:
  - `0x24` as active-row-count plus control count
  - `0x26` as row-area end or next free offset
  - `0x2e` as deleted/free-row-list head
  - `0x38` as object/storage/index identifier
- [ ] Decode file or extent bitmap pages enough to distinguish allocated and
  free pages.
- [ ] Decode page checksum or validation fields if present.
- [ ] Decode page SCN/LSN/checkpoint fields if present.
- [x] Reject sampled pages whose header identity does not match their file/page
  position in preflight.
- [x] Reject sampled same-file page references that point beyond the file page
  count in preflight.
- [x] Add page catalog scanner for page-kind counts, empty pages, page identity
  mismatches, and page-reference samples.
- [x] Add tentative page-kind labels to evidence output while preserving raw
  page kind values.
- [x] Add conservative same-file page-reference range diagnostics while
  preserving raw previous/next page references.
- [x] Expose selected anonymous page-header fields in page catalog samples for
  later free-space, slot-directory, and SCN/LSN analysis.
- [x] Add `page_type_raw`/`page_kind_raw` cross-counts to page catalogs and
  database summaries so known page classes can calibrate a preliminary
  first-byte PAGE type enum.
- [x] Add row-area probes to page catalog samples that compare the page-header
  row-count candidate with the physical row-length chain and deleted-row flags.
- [x] Aggregate row-area probe signals for sampled BTREE/data pages so count
  deltas and deleted-row pages are visible without inspecting every page sample.
- [x] Record neutral relations between anonymous page-header fields and
  row-chain facts to calibrate row count, free-space, and slot-directory
  candidates from evidence.
- [x] Add slot-tail candidate probing that scans post-row-chain bytes for
  2-byte page-offset values pointing to observed row starts.

## C. Segment And BTREE Structure

- [x] Confirm ordinary tables have `SYS.SYSINDEXES` BTREE storage entries.
- [x] Confirm table storage entry uses `GROUPID`, `ROOTFILE`, `ROOTPAGE`.
- [x] Confirm small tables can store rows directly in the root page.
- [x] Confirm larger tables move rows to linked leaf pages.
- [x] Capture segment root page-header identity and sampled candidate 6-byte
  page references in the target-table segment manifest.
- [x] Emit segment-root diagnostics for sampled candidate references that point
  to pages not currently classified as BTREE data pages.
- [x] Promote segment-root diagnostics to manifest-level diagnostics and final
  extraction reports.
- [x] Use segment manifest page-reference candidates plus same-file leaf
  `next_page` links as a conservative extraction page plan.
- [x] Exclude non-BTREE/data segment root/header pages from row scanning when
  BTREE/data leaf candidates are present in the manifest.
- [x] Emit page-plan diagnostics and avoid scanning planned or linked pages that
  are not classified as BTREE/data pages.
- [x] Emit extraction diagnostics for page-plan identity mismatches, out-of-range
  pages, cycles, and missing files referenced by page links.
- [x] Emit extraction diagnostics for unsupported column types before row
  scanning.
- [ ] Decode root page child/leaf pointers.
- [x] Traverse same-file linked leaf pages from a validated manifest leaf
  candidate without scanning the whole file.
- [x] Include all discovered files for the table's group/tablespace in the
  target-table segment manifest.
- [x] Traverse cross-file leaf `next_page` links when the referenced file is
  present in the segment manifest.
- [ ] Decode and traverse multi-level BTREE leaf chains.
- [ ] Decide fallback strategy: full segment scan when root traversal is not yet
  reliable.
- [ ] Decode how allocated extent lists are represented outside online views.
- [ ] Decode segment root/header metadata:
  - segment or storage object id
  - high-water mark or allocated page boundary
  - extent map/list roots
  - leaf chain anchors
- [ ] Verify traversal completeness and duplicate prevention for multi-page
  tables.
- [ ] Validate multi-file tablespace page references.

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
- [x] Query live `SYS.SYSCOLUMNS` structure and compare online column rows with
  copied `SYSTEM.DBF` raw rows after checkpoint.
- [ ] Replace the generic row payload offset with calibrated common
  row-metadata/NULL-control parsing, using both nullable user-table fixtures and
  `SYS.SYSCOLUMNS`.
- [x] Add heuristic offline scanner that finds SYSINDEXES-like candidates by
  storage index id directly in `SYSTEM.DBF`.
- [x] Validate SYSINDEXES-like scanner against controlled storage indexes
  `33595349..33595354`.
- [x] Add heuristic offline scanner that maps a table object id to child
  `INDEX<storage_id>` SYSOBJECTS candidates.
- [x] Validate child-index scanner against controlled table object ids
  `33629..33634`.
- [x] Emit a target-table dictionary and segment manifest that records
  `dm.ctl` file context, SYSTEM dictionary evidence, columns, storage index id,
  group/tablespace id, root file, and root page.
- [x] Attach matched `dm.ctl` DBF occurrence evidence to each data-file entry in
  the resolved segment manifest.
- [x] Emit segment-manifest diagnostics when a resolved data file lacks matched
  `dm.ctl` DBF occurrence evidence.
- [x] Preserve segment-manifest diagnostics in `extract-csv` reports for both
  `--segment-json` and `--database-dir` extraction paths.
- [x] Define the first three recovery steps as bootstrap:
  1. read the control file and recover database file structure;
  2. find the first SYSTEM tablespace file and dictionary table locations;
  3. dump dictionary information into `user.dict`, `tab.dict`, `col.dict`, and
     `file.dict`.
- [ ] Decode enough `SYSOBJECTS` rows offline to recover object name/id/schema/type.
- [ ] Decode complete `SYSCOLUMNS` row layout offline, including scale,
  nullability, defaults, and exact column id base.
- [ ] Decode complete `SYSINDEXES` row layout offline, including `KEYINFO` and
  extent allocation fields.
- [ ] Decode complete `SYSOBJECTS` child-index row layout, including exact `PID`,
  `TYPE$`, and `SUBTYPE$` offsets.
- [ ] Remove hard-coded dictionary root pages by discovering them from file
  metadata or a reliable system bootstrap structure.
- [ ] Decode schema/user dictionary metadata enough to resolve duplicate table
  names across schemas.
- [ ] Decode system data-file/tablespace dictionary metadata that backs
  `SYS.V$DATAFILE`.
- [ ] Detect unsupported table classes from dictionary metadata:
  partitioned, compressed, encrypted, LOB-heavy, HUGE, temporary, external, or
  non-row-store objects.

## E. Row Format And Type Decoding

- [x] Observe row length/status prefix.
- [x] Observe deleted-row high bit candidate in the row length/status prefix.
- [x] Implement physical row-chain scanner that can see deleted/updated row
  records beyond the page header's active-row count.
- [x] Observe fixed-width little-endian integer and double encodings.
- [x] Decode little-endian IEEE-754 `DOUBLE` values in the observed row path.
- [x] Observe short `VARCHAR` length encoding: one byte `0x80 + len` for 0..127.
- [x] Observe long `VARCHAR` length encoding: two-byte big-endian length for
  128 and above.
- [x] Model the currently observed row prefix as row length/status plus explicit
  metadata bytes and column payload offset.
- [ ] Decode row column directory, if present.
- [ ] Decode NULL bitmap/NULL column handling.
- [x] Reject non-zero observed row metadata bytes before column payload until
  NULL bitmap, column directory, and transaction flags are decoded.
- [ ] Decode `CHAR` padding rules.
- [ ] Decode signed integer family storage precisely:
  - `TINYINT`
  - `SMALLINT`
  - `INTEGER`
  - `BIGINT`
- [ ] Decode `DATE`.
- [ ] Decode `TIME`.
- [ ] Decode `TIMESTAMP`.
- [ ] Decode timestamp variants, including fractional precision and timezone
  variants if present in DM row-store tables.
- [ ] Decode `DECIMAL/NUMBER`.
- [ ] Decode `FLOAT` precisely and distinguish DM `FLOAT` from double storage.
- [ ] Decode LOB locator inline structure and decide when BLOB/CLOB data can be
  followed offline versus rejected as unsupported.
- [x] Add a field-trace path that slices observed row payloads using dictionary
  column metadata and records raw bytes for unsupported types such as
  `NUMBER`, `DATE`, `TIMESTAMP`, and LOBs instead of silently decoding them.
- [ ] Decode update and delete row status flags precisely.
- [ ] Decode row transaction/MVCC fields:
  - inserting/updating transaction id
  - commit/visibility SCN or equivalent
  - lock/active transaction marker
  - undo pointer
  - 19-byte row tail/control region observed after decoded payload
  - 6-byte SCN-like candidate at relative offset `12` in the row tail/control
    region
- [ ] Run synchronized SCN evidence capture:
  - query `DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER()`
  - force checkpoint
  - copy the target DBF immediately
  - search the DBF for direct SCN endian forms and dump affected page headers
    and row tails
  - repeat after one controlled insert/update/delete
- [ ] Distinguish row states in parser output:
  - visible live row
  - committed deleted row
  - committed old update version
  - uncommitted locked row
  - row that requires UNDO before-image lookup
- [ ] Decode row chaining, overflow rows, and LOB locators enough to detect or
  reject unsupported rows.

## F. MVCC, Transaction State, And UNDO

- [ ] Locate rollback/undo tablespace and files from offline metadata.
- [ ] Decode undo segment headers.
- [ ] Decode undo page headers.
- [ ] Decode undo record headers and before-image payloads.
- [ ] Decode how a row points to its undo record, including file/page/slot or
  equivalent address fields.
- [ ] Follow PRE IMAGE chains for updated/deleted rows and reconstruct the
  logically visible row image for the chosen extraction snapshot.
- [ ] Decode transaction table/status metadata.
- [ ] Build visibility rules for:
  - committed insert
  - committed delete
  - committed update
  - uncommitted insert
  - uncommitted delete
  - uncommitted update
  - rolled-back transaction
  - crash during transaction
- [ ] Decide extraction snapshot semantics:
  - latest clean checkpoint
  - supplied SCN
  - best-effort committed state
- [ ] Detect when redo is required and fail with a diagnostic until redo support
  exists.

## G. CSV Extractor

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
- [x] Surface decode failures in extraction reports instead of silently
  dropping unreadable rows.
- [x] Add stable extraction diagnostic code for live row decode failures.
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
- [ ] Add strict mode that fails if any live row, page, dictionary record, or
  transaction visibility decision is uncertain.
- [x] Emit extraction report artifact with row counts, skipped deleted rows,
  decode errors, and current consistency diagnostics.
- [ ] Extend extraction report artifact to include unsupported structures and
  transaction visibility diagnostics.
- [x] Allow `extract-csv` to consume the target-table segment manifest produced
  by `resolve-table --output`.
- [x] Emit scan-range fallback diagnostics for segment manifests that lack a
  page-reference plan.
- [x] Add `--strict-page-plan` so `extract-csv --segment-json` fails instead of
  silently using `scan_pages` fallback.

## H. Test Corpus

- [x] Create `DMDUL_T1`: mixed basic types, primary key.
- [x] Create `DMDUL_HEAP`: table without primary key.
- [x] Create `DMDUL_TYPES`: mixed scalar types.
- [x] Create `DMDUL_MANY`: multi-page table.
- [x] Create `DMDUL_ONE2`: single `INT` column.
- [x] Create `DMDUL_NULL2`: NULL pattern table.
- [x] Create `DMDUL_VLEN2`: `VARCHAR` length threshold table.
- [x] Create `DMDUL_DTTM2`: date/time/timestamp table.
- [x] Create `DMDUL_MOD2`: delete/update row-status table.
- [x] Add foundational fixture SQL covering tablespace/page/row/type/MVCC
  research scenarios.
- [x] Add raw page and marker evidence capture command for DBF files.
- [x] Add evidence manifest validation for copied file identity and capture
  JSON completeness.
- [ ] Create one-column tables for each target type.
- [ ] Create rows around page capacity boundaries.
- [ ] Create deleted rows followed by insert reuse.
- [ ] Create multi-extent table.
- [ ] Create multi-file tablespace table.
- [ ] Create duplicate table names in different schemas.
- [ ] Create committed update/delete/insert fixtures.
- [ ] Create uncommitted insert fixture copied before commit.
- [ ] Create uncommitted delete fixture copied before commit.
- [ ] Create uncommitted update fixture copied before commit.
- [ ] Create rollback fixture after insert/update/delete rollback.
- [ ] Create crash-state fixture with active transaction.
- [ ] Capture expected online `SELECT *` CSV for every fixture.
- [ ] Capture cold-consistent and crash-state file snapshots separately.
