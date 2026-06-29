# DM8 Storage Exploration Plan

This plan tracks the database-level investigation needed for `dmdul`. The goal
is not just to find row bytes in a file; it is to reconstruct enough DM8 storage
and transaction semantics to export a table correctly when the instance cannot
start.

See also:

- [Foundational research plan](FOUNDATIONAL_RESEARCH_PLAN.md)
- [Technical roadmap](TECHNICAL_EXPLORATION_ROADMAP.md)
- [Implementation checklist](EXPLORATION_TASKS.md)

## Phase 1: Storage Model Baseline

Goal: understand enough of DM8's physical storage model to build a focused
offline extractor prototype with explicit correctness boundaries.

Topics to document:

- Tablespace metadata and identifiers.
- Data file metadata and file-to-tablespace mapping.
- Segment metadata for table data objects.
- Page size, file header layout, page header layout, and page numbering.
- Extent or cluster allocation rules.
- Row storage format for ordinary BTREE tables.
- Differences between ordinary tables, indexes, partitioned tables, compressed
  tables, and HUGE tables.
- ASM disk group metadata and logical-to-physical file mapping.
- File-system and snapshot assumptions: whether the input files are from clean
  shutdown, crash state, copied live files, storage snapshot, sparse files, or
  ASM disks.
- Page checkpoint/LSN/SCN fields, checksums if present, and torn-page signals.
- MVCC fields in row records: transaction id, undo pointer, row version flags,
  delete/update flags, and commit visibility.
- UNDO tablespace, undo segment, undo page, and undo record formats.
- Transaction status metadata needed to distinguish committed, uncommitted,
  rolled-back, and in-progress row versions.

Important online metadata views:

- `DBA_TABLES`: table-level metadata.
- `DBA_SEGMENTS`: table segment metadata and storage object lookup.
- `DBA_DATA_FILES`: data file metadata.
- `DBA_TABLESPACES`: tablespace metadata.
- `V$TABLESPACE`: lower-level tablespace status and page-size-oriented fields.

## Phase 2: Controlled Test Table

Goal: create a small table whose row contents make byte-level page analysis
easy.

Suggested test shape:

```sql
create tablespace DMDUL_TS datafile 'DMDUL_TS01.DBF' size 64;

create table SYSDBA.DMDUL_T1 (
  ID int primary key,
  C_INT int,
  C_BIG bigint,
  C_VC varchar(64),
  C_DATE date,
  C_TS timestamp
) tablespace DMDUL_TS;

insert into SYSDBA.DMDUL_T1 values
  (1, 11, 1111111111, 'DMDUL_ROW_0001', date '2026-06-29', timestamp '2026-06-29 10:11:12'),
  (2, 22, 2222222222, 'DMDUL_ROW_0002', date '2026-06-30', timestamp '2026-06-30 10:11:12');

commit;
```

The exact syntax may need adjustment after checking the live DM8 instance.

## Phase 3: Locate Physical Storage

For the test table, capture:

- `DBA_TABLES` row.
- `DBA_SEGMENTS` row.
- tablespace id/name from `DBA_TABLESPACES` or `V$TABLESPACE`.
- data file id/path/size from `DBA_DATA_FILES`.
- object id and segment/page identifiers if available.

Expected metadata path:

```text
DBA_TABLES -> DBA_SEGMENTS -> DBA_TABLESPACES -> DBA_DATA_FILES
```

## Phase 4: Page Inspection

Use deterministic rows to find the table page in the data file:

- copy or read the target `.DBF` file while the database is stopped or from a
  safe snapshot if possible;
- search for marker strings such as `DMDUL_ROW_0001`;
- inspect nearby bytes to infer page boundaries;
- compare offsets with metadata page ids;
- document page header fields, slot directory, row offsets, row length, NULL
  bitmap, column length encoding, and fixed/variable column encoding.

## Phase 5: Prototype Extractor

Initial extractor scope:

- ordinary data files only;
- ordinary non-compressed BTREE table;
- metadata supplied from online dictionary export;
- scan one data file for pages belonging to one segment;
- decode a small set of common types: integer, bigint, varchar, date, timestamp.

ASM support should be added after ordinary file parsing is understood.

## Phase 6: Database Semantics

Goal: move from page scanning to a database-correct extractor.

Required investigations:

- Determine whether the copied files are cold-consistent or crash-state.
- Locate checkpoint/SCN/LSN metadata in file and page headers.
- Decode row MVCC metadata for insert, delete, update, rollback, and concurrent
  uncommitted transactions.
- Locate transaction tables and UNDO segment metadata.
- Decode enough UNDO records to reconstruct or reject row versions whose current
  page image is not visible.
- Decide the extraction snapshot rule: latest clean checkpoint, supplied SCN, or
  best-effort committed state.
- Add strict extraction mode that exits non-zero when any row or transaction
  visibility decision is uncertain.
