# DM8 Test Environment

This file records the initial test environment for the `dmdul` project.

## Host

- Server: `192.168.32.102`
- Hostname: `orcl`
- OS account: `dmdba`
- OS password: `NewDy2025`
- `DM_HOME`: `/opt/dmdbms`
- `disql`: `/opt/dmdbms/bin/disql`
- Running instance parameter file: `/dmdata/data/DAMENG/dm.ini`

## Database

- Database: DM8
- Observed server version: `DM Database Server 64 V8`, `8.10`,
  `DB Version: 0x7000c`, build `03134284194-20240703-234060-20108`
- Privileged client: `disql`
- SYSDBA connection:

```sh
disql 'SYSDBA/nhYW1]iBg]I!'
```

## Storage Model Notes

- DM uses an Oracle-like segment/page tablespace storage model.
- DM table storage is expected to be BTREE-based, similar in broad shape to
  MySQL InnoDB table structures.
- `DBA_TABLES` contains basic table metadata.
- `DBA_SEGMENTS` can be used to find the segment, meaning the physical data
  storage object, associated with a table.
- `DBA_DATA_FILES` contains data file metadata.
- `DBA_TABLESPACES` contains tablespace metadata.
- Observed ordinary tablespace page size: `8192` bytes.
- The project must support offline extraction from:
  - ordinary DM data files
  - DM ASM disk groups

## Project Objective

Build a `dmdul` tool similar in purpose to Oracle DUL:

- Operate when the DM database cannot start.
- Discover and parse DM tablespace files or ASM disk groups.
- Recover metadata required to identify tables and columns.
- Decode table pages and extract row data.
- Export recovered table data in a practical format such as CSV, SQL, or a
  structured intermediate format.

## Research Sources To Use

- Local Neo4j knowledge graph entries about DM/Dameng internals.
- Official or public Dameng documentation about:
  - tablespaces
  - data files
  - pages
  - segments and extents
  - heap/BTREE table storage
  - ASM disk group structure

## Security Note

The credentials above are test-environment secrets. Do not copy them into
source code, command history examples intended for publication, issue reports,
or generated release artifacts.
