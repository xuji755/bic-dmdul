# DM8 Dump Type Calibration - 2026-07-01

This note records the online `dump()` calibration work and the matching offline
row decoder fixes completed on 2026-07-01. It is intentionally evidence-focused:
`dump()` output is used as a calibration source, while DBF page bytes remain the
final source for offline extraction behavior.

## Goal

The immediate goal was to resolve two extraction blockers:

- data type decoding was incomplete for `DATE`, `TIME`, `TIMESTAMP`, `NUMBER`,
  `DECIMAL`, `CLOB`, `BLOB`, and related aliases;
- page planning still emitted fallback scan-range warnings for tables whose
  storage id was already known.

The missing-SYSTEM-file path remains out of scope for this phase.

## Online Calibration Method

A controlled table was created on the live DM8 test instance and each column was
queried through DM's `dump(col)` function. This gives DM's logical/internal type
code, byte length, and byte sequence for known SQL values.

Raw files generated during this run:

```text
tmp/dmdul_dump_types.sql
tmp/dmdul_dump_types.out
```

The fixture table was `SYSDBA.DMDUL_DUMP_TYPES`:

```sql
CREATE TABLE SYSDBA.DMDUL_DUMP_TYPES (
  ID INT PRIMARY KEY,
  C_TINY TINYINT,
  C_SMALL SMALLINT,
  C_INT INT,
  C_INTEGER INTEGER,
  C_BIG BIGINT,
  C_REAL REAL,
  C_FLOAT FLOAT,
  C_DOUBLE DOUBLE,
  C_NUMBER NUMBER(38),
  C_NUMERIC NUMERIC(30,10),
  C_DECIMAL DECIMAL(18,4),
  C_DATE DATE,
  C_TIME TIME(6),
  C_TIMESTAMP TIMESTAMP(6),
  C_DATETIME DATETIME(6),
  C_CHAR CHAR(8),
  C_VARCHAR VARCHAR(40),
  C_CLOB CLOB,
  C_BLOB BLOB
) TABLESPACE DMDUL_TS;
```

Three rows were inserted:

- row 1: small positive values and `2026-06-30 10:11:12.654321`;
- row 2: negative/boundary values and minimum date/time values;
- row 3: positive boundary values and zero numeric values.

`select checkpoint(0)` returned `0` before reading the DBF evidence.

## `dump()` Findings

Representative `dump()` findings:

| SQL type | dump type | dump length | Evidence |
| --- | ---: | ---: | --- |
| `TINYINT` | 7 | 4 | `1` => `1,0,0,0`; `-128` => `128,255,255,255` |
| `SMALLINT` | 7 | 4 | `2` => `2,0,0,0`; `32767` => `255,127,0,0` |
| `INT` / `INTEGER` | 7 | 4 | little-endian signed 32-bit |
| `BIGINT` | 8 | 8 | little-endian signed 64-bit |
| `REAL` | 10 | 4 | IEEE 754 single precision, little-endian |
| `FLOAT` | 11 | 8 | IEEE 754 double precision in this fixture |
| `DOUBLE` | 11 | 8 | IEEE 754 double precision, little-endian |
| `NUMBER` / `NUMERIC` / `DECIMAL` | 9 | variable | base-100 family; zero is `128`; negative numbers use low first byte and `102` terminator |
| `DATE` | 14 | 13 | `dump()` expands date to a 13-byte logical representation |
| `TIME(6)` | 15 | 13 | `dump()` includes date-like defaults plus time fields |
| `TIMESTAMP(6)` / `DATETIME(6)` | 16 | 13 | same logical shape in `dump()` |
| `CHAR` / `VARCHAR` | 2 | value length | raw character bytes; `CHAR` is space padded |
| `CLOB` | 19 | 64 | locator/control prefix plus inline short text tail in this fixture |
| `BLOB` | 12 | 55 | locator/control prefix plus inline short binary tail in this fixture |

Important distinction: `dump()` is not identical to DBF row bytes. For example,
DBF row evidence stores `DATE` as 3 bytes, `TIME` as 5 bytes, and
`TIMESTAMP`/`DATETIME` as 8 bytes, while `dump()` prints 13 bytes for these
logical values. Do not force DM `DATE` into an Oracle-style or documentation-only semantic
shape when recovering data. Current ordinary row evidence stores `DATE` in a
3-byte payload and the tested values decode as year/month/day. However,
`dump(DATE)` reports Typ=14 with a 13-byte logical representation that includes
additional field positions. If future page-row or dictionary evidence shows a
wider `DATE` payload, dmdul must preserve and decode those bytes rather than
discarding them because the visible SQL type name is `DATE`.

## Offline Row Layout Finding

The main decoder bug was not only missing type formulas. The previous decoder
read columns in SQL column order from left to right. Current DBF evidence shows
ordinary all-non-null rows are arranged as:

```text
row header / metadata
fixed-width column area
variable-width column area
row tail / control bytes
```

The SQL result order is still the dictionary column order, but the physical row
bytes are grouped by storage class.

Example from `DMDUL_NUM38_STORE`:

```text
SQL columns: ID INT, N38 NUMBER, D DATE, TS TIMESTAMP, MARKER VARCHAR
row bytes:   ID, D, TS, N38, MARKER, tail/control
```

If the decoder reads `N38` immediately after `ID`, it lands on `DATE` bytes and
misinterprets them as a variable-length NUMBER prefix. This produced errors such
as:

```text
row is too short while decoding column N38: offset=2, length=384
```

The fix is to decode fixed-width columns first, then variable-width columns, and
finally return values in SQL column order.

## Implemented Decoder Changes

Implemented in `src/dmdul/decode.py`:

- added support for `TINYINT`, `SMALLINT`, `INTEGER`, `DATE`, `TIME`,
  `TIMESTAMP`, `DATETIME`, `NUMBER`, `NUMERIC`, `DECIMAL`, `CLOB`, and `BLOB`;
- corrected `REAL` to 4-byte IEEE float;
- kept `FLOAT` as 4 bytes only when dictionary length is 4, otherwise 8 bytes;
- decoded the currently observed 3-byte page-row `DATE` format as:
  `year | (month << 15) | (day << 19)`; this is an evidence-specific decoder,
  not permission to discard extra Typ=14 fields if wider DATE payloads are found;
- decoded `TIME` from 5-byte page-row format:
  `hour | (minute << 5) | (second << 11) | (microsecond << 17)`;
- decoded `TIMESTAMP`/`DATETIME` as 3-byte date plus 5-byte time;
- decoded `NUMBER`/`NUMERIC`/`DECIMAL` using the observed base-100 payload;
- exported `CLOB`/`BLOB` as hex for now, because full LOB segment following is
  not implemented yet;
- changed row decoding to the fixed-area plus variable-area model.

The `NUMBER(38)` bug was fixed by converting base-100 digits directly into a
string and placing the decimal point from the exponent. This avoids precision
loss or accidental trailing zero insertion.

## Page Plan Change

Implemented in `src/dmdul/extract.py`:

- if dictionary metadata provides `storage_id`, page planning scans the segment
  window and accepts only pages where:
  - page kind is `0x14` BTREE data page;
  - page header file/page identity matches the physical page;
  - page header storage id matches the table storage id.

The resulting diagnostic is:

```text
page-plan-storage-id-scan
```

This replaced the earlier fallback diagnostic for known-storage-id tables:

```text
page-plan-fallback-scan-range
```

This is still a storage-id scan inside a segment window, not full extent bitmap
parsing. It is a correctness improvement and removes unrelated pages from the
planned page list, but exact extent-map decoding remains future work.

## Evidence Exports After Fix

The following evidence exports succeeded after the decoder and page-plan changes:

```sh
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src   python3 -m dmdul.cli --init-file tmp/e2e-init.dul   dump-data --table TEST2.DMDUL_DTTM2 --json

TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src   python3 -m dmdul.cli --init-file tmp/e2e-init.dul   dump-data --table TEST2.DMDUL_NUM38_STORE --json

TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src   python3 -m dmdul.cli --init-file tmp/e2e-init.dul   dump-data --table TEST2.DMDUL_TYPE_STORE --json
```

Results:

| Table | Rows | Status | Page plan diagnostic |
| --- | ---: | --- | --- |
| `TEST2.DMDUL_DTTM2` | 3 | OK | `page-plan-storage-id-scan` |
| `TEST2.DMDUL_NUM38_STORE` | 4 | OK | `page-plan-storage-id-scan` |
| `TEST2.DMDUL_TYPE_STORE` | 3 | OK | `page-plan-storage-id-scan` |

Representative successful values from `TEST2.DMDUL_NUM38_STORE`:

```text
1|0|0001-01-01|0001-01-01 00:00:00.000000|NUM38_ZERO_DATE_MIN
2|1|2000-01-01|2000-01-01 00:00:00.000000|NUM38_ONE_2000
3|12345678901234567890123456789012345678|2024-02-29|2024-02-29 23:59:59.123456|NUM38_POS_38
4|-12345678901234567890123456789012345678|2026-06-30|2026-06-30 10:11:12.654321|NUM38_NEG_38
```

Representative successful values from `TEST2.DMDUL_TYPE_STORE`:

```text
0 / negative / positive integer boundaries decoded correctly
NUMBER and DECIMAL positive/negative values decoded correctly
DATE/TIME/TIMESTAMP decoded correctly
CLOB/BLOB emitted as locator/inline payload hex
```

## Tests Added Or Updated

Updated tests cover:

- small integer aliases;
- `DATE`, `TIME`, `TIMESTAMP` decoding;
- base-100 `NUMBER` decoding, including 38-digit integer evidence;
- LOB payload hex export;
- fixed-area before variable-area row layout while preserving SQL result order;
- storage-id page planning;
- updated temporal block analysis expectations.

Full test command:

```sh
TMPDIR=/home/loop/dmdul/tmp PYTHONPATH=src python3 -m unittest discover -s tests
```

Result:

```text
Ran 124 tests in 0.198s
OK
```


## Export Fidelity Rule

The extractor must preserve what is present in the data file. A decoded value is
acceptable only when the mapping from bytes to value is understood and does not
drop information. If a payload contains extra fields, unknown control bytes, LOB
locators, timezone bytes, or any undecoded suffix, dmdul must export those bytes
verbatim, normally as raw hex, rather than trimming, escaping, summarizing, or
normalizing them away.

This rule applies especially to date/time types: do not discard zero or nonzero
fields just because the SQL type name suggests a narrower semantic value. The
page payload determines what must be exported.


## Date/Time Type Internal Codes - 2026-07-01 Follow-up

A focused table `SYSDBA.DMDUL_TIME_TYPES` was created to compare logical
`dump()` type codes, dictionary metadata, and DBF page-row payload widths. Raw
files from this run:

```text
tmp/dmdul_time_types.sql
tmp/dmdul_time_types.out
tmp/dmdul_time_types_pages.bin
```

The table used these columns:

```text
ID INT,
C_DATE DATE,
C_TIME0 TIME(0),
C_TIME6 TIME(6),
C_TS0 TIMESTAMP(0),
C_TS6 TIMESTAMP(6),
C_DT0 DATETIME(0),
C_DT6 DATETIME(6),
C_TSTZ TIMESTAMP(6) WITH TIME ZONE,
C_TSLTZ TIMESTAMP(6) WITH LOCAL TIME ZONE,
C_TMTZ TIME(6) WITH TIME ZONE
```

Observed mapping:

| SQL/dictionary type | `dump()` Typ | `dump()` Len | DBA column length | Page-row payload | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `DATE` | 14 | 13 | 3 | 3 | Current page payload is packed date bytes. `dump()` exposes a wider logical structure with time fields zero in this evidence. Do not discard wider fields if found in page bytes later. |
| `TIME(0)` / `TIME(6)` | 15 | 13 | 5 | 5 | Packed time bytes. Fractional precision affects stored microsecond bits, not payload width. |
| `TIMESTAMP(0)` / `TIMESTAMP(6)` | 16 | 13 | 8 | 8 | 3-byte date + 5-byte time. |
| `DATETIME(0)` / `DATETIME(6)` | 16 | 13 | 8 | 8 | Same observed payload shape and Typ as TIMESTAMP. |
| `TIMESTAMP WITH LOCAL TIME ZONE` | 16 | 13 | 8 | 8 | Dictionary scale observed as `4102`; page-row payload is normalized 8-byte timestamp in this evidence. |
| `TIME WITH TIME ZONE` | 22 | 13 | 7 | 7 | 5-byte time + 2-byte timezone offset. |
| `DATETIME WITH TIME ZONE` / `TIMESTAMP WITH TIME ZONE` | 23 | 13 | 10 | 10 | 8-byte timestamp + 2-byte timezone offset. DM dictionary reports this column as `DATETIME WITH TIME ZONE`. |

For row 1 inserted with `2026-07-01 13:14:15.654321 +08:00`, the page-row
payload split was:

```text
C_DATE   len=3  hex=ea870b
C_TIME0  len=5  hex=cd79000000
C_TIME6  len=5  hex=cd79e2f713
C_TS0    len=8  hex=ea870bcd79000000
C_TS6    len=8  hex=ea870bcd79e2f713
C_DT0    len=8  hex=ea870bcd79000000
C_DT6    len=8  hex=ea870bcd79e2f713
C_TSTZ   len=10 hex=ea870bcd79e2f713e001
C_TSLTZ  len=8  hex=ea870bcd79e2f713
C_TMTZ   len=7  hex=cd79e2f713e001
```

The trailing timezone bytes `e0 01` are little-endian decimal `480`, matching
`+08:00` in minutes. This is a direct storage observation, not a general rule for
all timezone encodings until more offsets are tested.

The key rule from this evidence is that `dump()` Typ identifies the logical DM
internal type family, while `DBA_TAB_COLUMNS.DATA_LENGTH` matched the observed
page-row payload width for these fixed-width date/time columns. Export code
should use page bytes and dictionary length together and must preserve raw bytes
for any unrecognized suffix or variant.

## Current Limits

Known remaining limits after this work:

- `CLOB`/`BLOB` are not yet followed into full LOB storage; current output is
  raw locator/inline payload hex.
- Rows with nonzero row metadata, NULL bitmap variants, or more complex row
  control structures are still rejected until their metadata is decoded.
- Page planning now filters by storage id and data-page kind, but does not yet
  parse the segment root / extent bitmap into an exact extent list.
- Missing `SYSTEM.DBF` recovery mode is still deferred.

## Practical Rule Going Forward

For future type work, use paired online/offline evidence:

1. create a deterministic test table;
2. insert boundary values;
3. query both SQL value and `dump(col)`;
4. checkpoint and copy/read the DBF page;
5. correlate row offsets, raw bytes, and decoded output;
6. add a focused regression test before broadening the decoder.
