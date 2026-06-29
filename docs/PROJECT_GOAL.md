# dmdul Project Goal

## User-Facing Goal

When a DM8 database instance cannot start, but the database data files are
intact, `dmdul` should extract all rows from a specified user table and write a
CSV file.

The target workflow is:

```sh
dmdul extract-csv \
  --database-dir /dmdata/data/DAMENG \
  --table SYSDBA.T1 \
  --output T1.csv
```

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
- LOB and long row storage;
- compressed tables;
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

