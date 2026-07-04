# bic-dmdul Project Goal

## User-Facing Goal

When a DM8 database instance cannot start, but the database data files are
intact, `bic-dmdul` should extract all rows from a specified user table and write a
CSV file.

The target workflow is:

```sh
bic-dmdul extract-csv \
  --database-dir /dmdata/data/DAMENG \
  --table SYSDBA.T1 \
  --output T1.csv
```

## Correctness Goal

`bic-dmdul` is a database-level offline reader, not a byte-pattern file dumper. A
successful extraction must:

- identify the database, tablespaces, files, segments, pages, dictionary rows,
  table rows, and transaction visibility from on-disk structures;
- return each logically visible row exactly once;
- skip deleted or rolled-back row versions;
- avoid returning uncommitted row versions when their transaction state can be
  determined from on-disk metadata;
- fail loudly when a required storage, dictionary, row, MVCC, or UNDO structure
  is not understood well enough to guarantee correctness.

The implementation should therefore prefer "known incomplete" over silent
partial output. Transitional research modes may scan pages heuristically, but
the production path must expose any uncertainty in the extraction report.

## Milestone Semantics

The project has two correctness milestones:

1. **Cold-consistent extraction**: data files come from a clean shutdown,
   storage snapshot, or otherwise internally consistent checkpoint. The tool can
   ignore crash recovery but must still understand table storage, page layout,
   row format, and committed delete/update markers.
2. **Crash-state extraction**: data files may contain active transaction
   versions. The tool must understand row MVCC fields, transaction status, and
   UNDO enough to decide which row version is visible at the chosen extraction
   point.

Redo log replay is outside the first milestone. If data files require redo to
reach a consistent committed state, `bic-dmdul` should detect the condition and
report it rather than fabricating rows.

## Non-Negotiable Constraint

The final extraction path must not query online views such as:

- `DBA_TABLES`
- `DBA_SEGMENTS`
- `DBA_EXTENTS`
- `DBA_DATA_FILES`
- `DBA_TABLESPACES`

Those views are allowed only during research to calibrate binary structures.
The offline tool must recover equivalent metadata from data files and SYS
dictionary tables.

## Initial Supported Scope

Version 1 should support:

- intact ordinary DM8 data files;
- ordinary row-store BTREE tables;
- non-compressed, non-encrypted table data;
- non-partitioned tables;
- fixed and variable scalar columns:
  - `INT`
  - `BIGINT`
  - `VARCHAR`
  - `CHAR`
  - `DATE`
  - `TIME`
  - `TIMESTAMP`
  - selected numeric/float types after encoding is verified;
- deleted rows must be skipped;
- output must be valid CSV with column headers.

Version 1 may require the user to provide the database directory and target
table name.

## Later Scope

Later versions should handle:

- ASM disk groups;
- partitioned tables;
- secondary index assisted lookup;
- additional LOB variants and cross-file LOB chains;
- chained rows that span multiple data blocks; `STORAGE(USING LONG ROW)` out-of-line variable columns are already supported through verified `0x22` pages;
- active-slot migrated-row pointer detection and skip logic; old physical rows outside the slot directory are already skipped by the current active-row path;
- compressed HUGE variants that require `$AUX.CPR_FLAG='Y'` column-section decompression, including `QUERY HIGH` and column-level compression;
- encrypted tablespaces;
- damaged but partially readable data files;
- dictionary bootstrap without any hard-coded system table root pages.

## Required Offline Metadata Path

The extractor must build enough dictionary metadata from files:

1. Identify data files and their group/tablespace ids.
2. Identify `SYSTEM.DBF`.
3. Locate and decode core SYS dictionary BTREEs:
   - `SYS.SYSOBJECTS`
   - `SYS.SYSCOLUMNS`
   - `SYS.SYSINDEXES`
4. Resolve target table:
   - schema name
   - table name
   - object id
   - column list and column types
   - table storage root: `GROUPID`, `ROOTFILE`, `ROOTPAGE`
5. Traverse or scan the table BTREE pages.
6. Decode rows.
7. Write CSV.

During development, a temporary "calibrated metadata" mode may accept metadata
exported from a healthy database. That mode is only a stepping stone for row and
page decoding; it is not the final DUL-style solution.
