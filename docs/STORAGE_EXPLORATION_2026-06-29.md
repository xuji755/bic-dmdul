# DM8 Storage Exploration: 2026-06-29

This note records the first live exploration on the DM8 test instance at
`192.168.32.102`.

## Environment Facts

- Hostname: `orcl`
- OS user: `dmdba`
- `DM_HOME`: `/opt/dmdbms`
- `disql`: `/opt/dmdbms/bin/disql`
- DM server process:
  - `/opt/dmdbms/bin/dmserver path=/dmdata/data/DAMENG/dm.ini -noconsole`
- Instance status from `disql`: ordinary open state.
- Version:
  - `DM Database Server 64 V8`
  - `8.10`
  - enterprise edition
  - `DB Version: 0x7000c`
  - build `03134284194-20240703-234060-20108`

## Dictionary View Fields

`DBA_TABLES` has the expected Oracle-compatible table metadata fields:

- `OWNER`
- `TABLE_NAME`
- `TABLESPACE_NAME`
- `NUM_ROWS`
- `BLOCKS`
- `SEGMENT_CREATED`
- compression and temporary-table related flags

`DBA_SEGMENTS` is the first useful bridge from logical table to physical
storage:

- `OWNER`
- `SEGMENT_NAME`
- `SEGMENT_TYPE`
- `TABLESPACE_NAME`
- `HEADER_FILE`
- `HEADER_BLOCK`
- `RELATIVE_FNO`
- `BYTES`
- `BLOCKS`
- `EXTENTS`

`DBA_DATA_FILES` maps tablespaces to files:

- `FILE_NAME`
- `FILE_ID`
- `TABLESPACE_NAME`
- `BYTES`
- `BLOCKS`
- `RELATIVE_FNO`
- `USER_BLOCKS`
- `STATUS`

`DBA_TABLESPACES` exposes the logical page size:

- `TABLESPACE_NAME`
- `BLOCK_SIZE`
- `STATUS`
- `CONTENTS`

`V$TABLESPACE` exposes lower-level tablespace identifiers and page counts:

- `ID`
- `NAME`
- `TYPE$`
- `STATUS$`
- `TOTAL_SIZE`
- `FILE_NUM`
- `USED_SIZE`
- `FREE_EXTENTS`

## Existing Tablespaces

Observed tablespaces before creating the test object:

| Tablespace | Contents | Block size | Files |
| --- | --- | ---: | ---: |
| `SYSTEM` | permanent | 8192 | 1 |
| `ROLL` | undo | 8192 | 1 |
| `TEMP` | temporary | 8192 | 1 |
| `MAIN` | permanent | 8192 | 3 |
| `SYSAUX` | permanent | 8192 | 3 |

Data files are under `/dmdata/data/DAMENG`.

## Test Object

Created a dedicated tablespace and table:

```sql
create tablespace DMDUL_TS
  datafile '/dmdata/data/DAMENG/DMDUL_TS01.DBF'
  size 64;

create table SYSDBA.DMDUL_T1 (
  ID int primary key,
  C_INT int,
  C_BIG bigint,
  C_VC varchar(64),
  C_DATE date,
  C_TS timestamp
) tablespace DMDUL_TS;

insert into SYSDBA.DMDUL_T1 values
  (1, 11, 1111111111, 'DMDUL_ROW_0001',
   date '2026-06-29', timestamp '2026-06-29 10:11:12');

insert into SYSDBA.DMDUL_T1 values
  (2, 22, 2222222222, 'DMDUL_ROW_0002',
   date '2026-06-30', timestamp '2026-06-30 10:11:12');

commit;
```

The row count after commit was `2`.

## Test Table Metadata

`DBA_OBJECTS`:

| Owner | Object | Type | Object id |
| --- | --- | --- | ---: |
| `SYSDBA` | `DMDUL_T1` | `TABLE` | 33626 |

`DBA_TAB_COLUMNS`:

| Column | Type | Length | Nullable |
| --- | --- | ---: | --- |
| `ID` | `INT` | 4 | `N` |
| `C_INT` | `INT` | 4 | `Y` |
| `C_BIG` | `BIGINT` | 8 | `Y` |
| `C_VC` | `VARCHAR` | 64 | `Y` |
| `C_DATE` | `DATE` | 3 | `Y` |
| `C_TS` | `TIMESTAMP` | 8 | `Y` |

`DBA_SEGMENTS` for the table:

| Segment | Type | Tablespace | Header file | Header block | Blocks | Extents |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `DMDUL_T1` | `TABLE` | `DMDUL_TS` | 0 | 16 | 16 | 1 |

`DBA_DATA_FILES` for the test tablespace:

| File | Relative file | Bytes | Blocks | User blocks |
| --- | ---: | ---: | ---: | ---: |
| `/dmdata/data/DAMENG/DMDUL_TS01.DBF` | 1 | 67108864 | 8192 | 8144 |

`V$TABLESPACE` for the test tablespace:

| ID | Name | Total pages | Used pages | Free extents |
| ---: | --- | ---: | ---: | ---: |
| 6 | `DMDUL_TS` | 8192 | 48 | 509 |

## First Physical Page Observation

The page size is `8192` bytes. `DBA_SEGMENTS.HEADER_BLOCK=16` maps to file
offset:

```text
16 * 8192 = 131072 = 0x20000
```

Searching the data file for the marker values found both inserted rows on this
page:

| Marker | File offset | Page offset |
| --- | ---: | ---: |
| `DMDUL_ROW_0001` | 131202 | 130 |
| `DMDUL_ROW_0002` | 131267 | 195 |

The first 64 bytes of the page at offset `0x20000` were:

```text
06 00 00 00 10 00 00 00 ff ff ff ff ff ff ff ff
ff ff ff ff 14 00 00 00 c1 04 e8 6e 4d 71 a5 a4
05 00 00 00 04 00 e4 00 00 00 00 00 02 00 ff ff
52 00 5a 00 00 00 f4 00 00 00 d1 9f 00 02 06 00
```

Initial row-area observation:

```text
... 00 41 00 00 01 00 00 00 0b 00 00 00 c7 35 3a 42
00 00 00 00 ea 07 eb ea 07 eb 6a 61 00 00 00 8e
44 4d 44 55 4c 5f 52 4f 57 5f 30 30 30 31 ...

... 00 41 00 00 02 00 00 00 16 00 00 00 8e 6b 74 84
00 00 00 00 ea 07 f3 ea 07 f3 6a 61 00 00 00 8e
44 4d 44 55 4c 5f 52 4f 57 5f 30 30 30 32 ...
```

Notes:

- The `INT` values appear little-endian in the row area:
  - `01 00 00 00`
  - `0b 00 00 00`
  - `02 00 00 00`
  - `16 00 00 00`
- The `BIGINT` values also appear little-endian:
  - `1111111111` -> `c7 35 3a 42 00 00 00 00`
  - `2222222222` -> `8e 6b 74 84 00 00 00 00`
- The string marker is stored inline in clear text.
- The byte before each marker is `8e`; this may be a variable-column length or
  type/length marker and needs more controlled cases.
- The two rows are separated by 65 bytes in this test page.

## Open Questions

- Whether `HEADER_BLOCK` is zero-based or file-header-adjusted in every
  tablespace/file case. The first test supports direct zero-based page offset:
  `HEADER_BLOCK * BLOCK_SIZE`.
- Why `DBA_SEGMENTS` reported an index segment and the table segment both at
  `HEADER_BLOCK=16`; this may be a compatibility-view artifact or a clue about
  DM's BTREE table organization.
- Exact page header field meanings.
- Exact row header, column count, NULL bitmap, variable-length column encoding,
  and date/timestamp encoding.
- Whether row data is always present in the segment header page for very small
  ordinary tables.

## Next Tests

- Insert rows with `NULL` values to identify NULL bitmap or column skip rules.
- Insert short and long `VARCHAR` values to decode the variable-length marker.
- Insert boundary numeric values, including negative integers.
- Create a table without a primary key to compare table/index segment layout.
- Create enough rows to allocate multiple data pages and a second extent.
- Export dictionary metadata to a local JSON fixture for the first parser.

## Later Dictionary Bootstrap Result

Raw `SYSTEM.DBF` scanning can now recover controlled table column definitions
from `SYSCOLUMNS`-like records when the table object id is known. The validated
pattern is object id bytes followed by a zero-based column id, declared length,
then nearby prefixed column-name and type-name strings.

For example, object id `33629` (`DMDUL_MANY`) yielded:

| Raw column id | Name | Type | Length | Page |
| ---: | --- | --- | ---: | ---: |
| 0 | `ID` | `INT` | 4 | 3059 |
| 1 | `MARKER` | `VARCHAR` | 64 | 3059 |
| 2 | `PAD` | `VARCHAR` | 3000 | 3059 |

The same scanner matched online `DBA_TAB_COLUMNS` for `DMDUL_ONE2`,
`DMDUL_NULL2`, `DMDUL_VLEN2`, `DMDUL_DTTM2`, and `DMDUL_MOD2`. Online
`COLUMN_ID` is one-based, while the raw value observed so far is zero-based.

`SYSINDEXES` storage roots are also recoverable by scanning `SYSTEM.DBF` for the
storage index object id. For example, index id `33595350` yielded a compact row
on page 5642:

```text
id=33595350, unique=N, groupid=6, rootfile=0, rootpage=144, type=BT, flag=1
```

The controlled storage indexes `33595349..33595354` all matched online
`SYS.SYSINDEXES` root pages. `SYS.SYSOBJECTS` also showed the bridge from table
object to storage index object: `INDEX33595350` has `ID=33595350`,
`TYPE$=TABOBJ`, `SUBTYPE$=INDEX`, and `PID=33630`.

The same bridge is now recoverable offline from `SYSTEM.DBF`: scanning for table
object ids `33629..33634` and selecting the nearest following `TABOBJ` plus
`INDEX<digits>` string produced highest-scoring candidates
`33595349..33595354`. These candidates were found on page 5697.
