# dmdul Technical Exploration Roadmap

This roadmap defines the technical work needed to make `dmdul` a correct DM8
offline table extractor. It intentionally treats DM8 as a database engine with
storage and transaction semantics, not as a collection of files containing row
strings.

## Target Guarantee

For a supported ordinary row-store table, `dmdul extract-csv` must produce a CSV
containing every logically visible row exactly once, with correct column values
and headers. It must skip deleted, rolled-back, and uncommitted row versions
when that state can be determined. If a required structure is unknown, the tool
must fail in strict mode instead of silently returning partial data.

## Required Bootstrap Workflow

The implementation must follow this offline recovery sequence:

1. Read the copied control metadata and data-file headers, then write
   `control.ctl` as `tablespace_id,file_id,full_local_path`. This file is an
   extraction-time map and may be generated from `dm.ctl`, generated from copied
   DBF headers, or hand-written when the original control file is unavailable.
2. Identify the SYSTEM data file that stores dictionary tables, then extract
   dictionary rows for USER, TABLE, and COLUMN into `user.dict`, `tab.dict`, and
   `col.dict`. Target-table name scans are only an interim heuristic, not a
   complete bootstrap.
3. Decode data block structures before trusting row extraction. The page parser
   must identify page kind, segment/table identity, relevant transaction fields,
   and page-to-page references.
4. Decode row structures and type storage before broad CSV extraction. The row
   parser must distinguish live, deleted, updated, locked, and undo-dependent
   rows, and must decode ordinary scalar types plus reject or follow LOB/overflow
   structures according to explicit evidence.

## Correctness Tiers

Tier 1: cold-consistent files.

- Input files come from clean shutdown, a storage snapshot, or a checkpointed
  copy known not to require redo.
- The extractor must understand data files, tablespaces, segments, pages, row
  layout, dictionary tables, and committed delete/update markers.
- UNDO/MVCC fields may be decoded enough to recognize unsupported states and
  fail when active transaction versions are present.

Tier 2: crash-state files.

- Input files may contain uncommitted or partially rolled-back row versions.
- The extractor must identify transaction state and use UNDO to decide row
  visibility at a chosen extraction point.
- Redo replay is not required initially, but the extractor must detect when redo
  is necessary for correctness.

## Exploration Domains

### 1. Input File Set And Filesystem Semantics

- Identify all required files in a database directory: ordinary `.DBF` files,
  system/control metadata, rollback/undo files, temp files, and log files if
  present.
- Determine which files are required for Tier 1 and Tier 2.
- Record file identity fields: database id, tablespace/group id, file number,
  creation/checkpoint fields, page size, total page count, and status.
- Detect unsafe copies: live non-snapshot copy, size mismatch, mixed checkpoint
  generations, missing files, sparse holes, unreadable pages, and torn writes.
- Decide how to report unsupported inputs before extraction starts.

### 2. Tablespace And Data File Layout

- Decode tablespace/group metadata from file headers and system dictionary
  records.
- Map logical tablespace ids to one or more data files.
- Decode file page 0 and early control pages:
  - page size or page-size code;
  - group id and file id;
  - checkpoint/SCN/LSN fields;
  - total/free page counters;
  - file status and compatibility flags;
  - checksum or validation fields if present.
- Decode file-level bitmap or space-management pages well enough to distinguish
  allocated, free, and reserved pages.
- Handle multi-file tablespaces and file-number references consistently.

### 3. Segment, Extent, And Allocation Metadata

- Decode `SYS.SYSINDEXES` storage rows beyond `GROUPID`, `ROOTFILE`, and
  `ROOTPAGE`.
- Identify extent/cluster allocation metadata for ordinary table segments.
- Determine how a segment maps to extents and pages without relying on
  `DBA_SEGMENTS` or `DBA_EXTENTS`.
- Decode root/header pages for segment metadata:
  - segment id or object id;
  - root/internal/leaf pointers;
  - high-water mark or allocated page list;
  - extent map or linked allocation pages;
  - row count or active slot count if stored.
- Define a fallback full-segment scan only after allocation boundaries are known.

### 4. Page Format

- Build a page parser for every page kind needed by table extraction:
  - file/control page;
  - bitmap/space-management page;
  - segment root/header page;
  - BTREE internal page;
  - BTREE leaf/data page;
  - UNDO page;
  - free/empty initialized page.
- Decode common page-header fields:
  - group/file/page identity;
  - page kind;
  - previous/next page references;
  - object or segment id;
  - row/slot count;
  - free-space start/end;
  - slot directory offset;
  - page SCN/LSN/checkpoint;
  - checksum or corruption markers.
- Validate page references and reject pages whose identity does not match their
  file position.

### 5. Dictionary Bootstrap

- Locate `SYSTEM.DBF` and dictionary roots without hard-coded page numbers.
- Decode core dictionary tables:
  - `SYS.SYSOBJECTS` for schema/object ids, names, object types, parent ids;
  - `SYS.SYSCOLUMNS` for column order, type, length, scale, precision,
    nullability, defaults, and hidden columns;
  - `SYS.SYSINDEXES` for table storage roots, BTREE metadata, key info, and
    allocation fields;
  - system data-file/tablespace metadata source used by `SYS.V$DATAFILE`.
- Resolve owner and table names unambiguously, including duplicate names across
  schemas.
- Detect unsupported table classes: partitioned, compressed, encrypted, LOB,
  external, temporary, HUGE, or column-store variants.

### 6. BTREE And Table Traversal

- Decode table BTREE root, internal, and leaf pages.
- Traverse leaf pages from the root instead of scanning arbitrary page ranges.
- Understand leaf sibling links and verify traversal completeness.
- Determine whether ordinary tables without primary keys use the same storage
  organization as primary-key tables.
- Decode rowid or physical key fields if they are stored separately from user
  columns.
- Avoid duplicate rows when pages are reachable through multiple paths or stale
  links.

### 7. Row Format And Data Types

- Decode the physical row header:
  - length/status bits;
  - row version flags;
  - deleted/update flags;
  - column count;
  - null bitmap;
  - column directory;
  - transaction/MVCC fields;
  - undo pointer if present.
- Decode scalar types in controlled one-column and mixed-column tables:
  - `INT`, `BIGINT`;
  - `CHAR`, `VARCHAR`, long variable values;
  - `DATE`, `TIME`, `TIMESTAMP`, `DATETIME`;
  - `DECIMAL`, `NUMBER`, precision/scale variants;
  - `FLOAT`, `DOUBLE`, `REAL`;
  - binary types where reasonable.
- Decode row chaining, overflow, and LOB locators enough to detect unsupported
  rows or follow them in later milestones.
- Preserve CSV correctness: NULL versus empty string, character padding,
  encoding, delimiter escaping, and deterministic column order.

### 8. MVCC, Transaction State, And UNDO

- Identify row-level transaction metadata:
  - creator/updater transaction id;
  - commit sequence or visibility SCN;
  - lock or active transaction marker;
  - delete/update marker;
  - undo segment/page/slot pointer.
- Locate transaction tables or rollback segment metadata on disk.
- Decode UNDO tablespace and undo segment structures:
  - undo segment headers;
  - undo page headers;
  - undo record headers;
  - before-image payloads;
  - links between undo records;
  - transaction status fields.
- Build visibility rules for:
  - committed insert;
  - committed delete;
  - committed update;
  - uncommitted insert;
  - uncommitted delete;
  - uncommitted update;
  - rollback;
  - crash during transaction.
- Decide when redo is required and make that a hard diagnostic in Tier 2 until
  redo support exists.

### 9. Validation Corpus

Create controlled tables and file snapshots for every storage state:

- tiny table with rows in root page;
- multi-page table with linked leaves;
- multi-extent and multi-file tablespace table;
- table without primary key;
- duplicate schema/table names;
- NULL combinations across fixed and variable columns;
- each scalar data type at boundary values;
- deletes, updates, and slot reuse;
- committed and uncommitted transactions at copy time;
- rollback after update/delete/insert;
- crash-like copy with active transaction;
- corrupted or missing page negative tests.

For every fixture, capture:

- online `SELECT *` expected output;
- online dictionary metadata used only for calibration;
- copied data files;
- page offsets and decoded structure notes;
- whether the fixture belongs to Tier 1 or Tier 2.

## Implementation Order

1. Build the controlled fixture corpus and raw-byte evidence set.
2. Decode file, tablespace, segment, page, and record structures from fixtures.
3. Decode scalar type encodings from one-column and mixed-column fixtures.
4. Decode row MVCC metadata and transaction status markers.
5. Decode UNDO enough to follow PRE IMAGE chains for updates/deletes.
6. Only then replace page-range scans with verified segment/BTREE traversal.
7. Replace dictionary heuristics with decoded dictionary rows.
8. Add extraction reporting and strict failure modes around proven structures.

## Stop Conditions

The extractor must stop and report a diagnostic when:

- a required data file is missing or belongs to a different database/checkpoint;
- a page checksum/identity/SCN check fails and no recovery rule is implemented;
- a page kind needed for traversal is unknown;
- a row type, column type, NULL bitmap, or column directory cannot be decoded;
- a row has active MVCC state but transaction/UNDO visibility is unknown;
- redo appears necessary to make the file set consistent.
