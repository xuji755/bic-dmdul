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
- initially exported unresolved `CLOB`/`BLOB` payloads as hex. Later work added
  inline and out-of-line LOB following; see the LOB Attachment Export section
  below for current behavior;
- changed row decoding to the fixed-area plus variable-area model;
- decoded the observed ordinary-row NULL bitmap: two little-endian bits per
  storage-order column, `00` for present and `11` for NULL. Fixed-width NULL
  columns still consume fixed bytes; variable-width NULL columns omit their
  length prefix and payload.

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

## Type Coverage Extension - 2026-07-03

`DBA_TAB_COLUMNS.DATA_TYPE` on the DM8 lab instance currently exposes these
ordinary target types:

```text
BIGINT, BLOB, BYTE, CHAR, CLOB, DATE, DATETIME,
DATETIME WITH TIME ZONE, DEC, DECIMAL, DOUBLE, FLOAT, INT, INTEGER,
INTERVAL DAY TO SECOND, NUMBER, NUMERIC, REAL, ROWID, SMALLINT, TEXT,
TIME, TIME WITH TIME ZONE, TIMESTAMP, TIMESTAMP WITH LOCAL TIME ZONE,
TINYINT, VARBINARY, VARCHAR, VARCHAR2
```

`CLASS234882066` also appears in dictionary views, but it is an internal class
type and is not treated as an ordinary table extraction target.

Focused remote fixtures validated the remaining type families:

- `DMDUL_TYPE_COVER5` covered `DEC`, `BYTE`, `VARBINARY`, `TEXT`,
  `TIME WITH TIME ZONE`, `TIMESTAMP WITH LOCAL TIME ZONE`,
  `DATETIME WITH TIME ZONE`, and `INTERVAL DAY TO SECOND`.
- `DMDUL_ROWID_COVER` covered a real `ROWID` column populated from a table
  pseudo-rowid.

Observed storage additions:

- `BYTE` is fixed width 1 byte in ordinary rows. dmdul emits it as two hex
  digits, preserving the byte value instead of converting it to a numeric
  display.
- `INTERVAL DAY TO SECOND` is fixed width 24 bytes in the tested row. The first
  five little-endian signed 32-bit fields decode as day, hour, minute, second,
  and microsecond. The sixth 32-bit field is retained as metadata for now.
- `ROWID` is fixed width 12 bytes. DM renders it as three 4-byte big-endian
  integers, each encoded into six base64-style characters. The observed raw
  value `00 00 00 00 00 00 00 00 00 00 00 01` renders as
  `AAAAAAAAAAAAAAAAAB`.
- Short `TEXT`, `CLOB`, and `BLOB` values may carry a 13-byte inline LOB prefix:
  one flag byte, eight locator/control bytes, and a 4-byte inline payload
  length. When this length equals the remaining payload length, dmdul emits only
  the inline value.
- The lab database stores non-ASCII character bytes in the GB18030 family. The
  decoder treats ASCII directly, then tries GB18030 before UTF-8 for non-ASCII
  bytes.

Remote strict extraction evidence:

```text
SYSDBA.DMDUL_TYPE_COVER5
1,42.5,7f,cafe01,TEXT_VALUE_一,13:14:15.654321 +08:00,
2026-07-01 13:14:15.654321,
2026-07-01 13:14:15.654321 +08:00,1 02:03:04.500000

SYSDBA.DMDUL_ROWID_COVER
1,AAAAAAAAAAAAAAAAAB
```

The local coverage helper now reports zero unsupported observed ordinary target
types, excluding the internal `CLASS234882066`.

## Current Limits

Known remaining limits after this work:

- `CLOB`/`BLOB`/`TEXT` short inline payloads are decoded. The currently verified
  21-byte out-of-line locator shape is followed through `0x20` LOB data pages.
  Unknown locator shapes or failed page-chain validation are preserved as raw
  locator evidence rather than silently truncated.
- Row metadata states outside the observed NULL bitmap values `00` and `11`, or
  extra metadata bits beyond the column count, are still rejected until column
  directory or transaction-control semantics are decoded.
- Page planning now filters by storage id and data-page kind, but does not yet
  parse the segment root / extent bitmap into an exact extent list.
- Missing `SYSTEM.DBF` recovery now has an explicit storage-scan mode:
  `bootstrap --scan-storages-without-system-dicts` writes `storage_scan.dict`
  and `SCAN.TAB_<storage_id>` placeholders, and
  `dump-data --scan-storage-dict` exports raw physical row bytes.

## Remote CLI Validation Follow-up

The remote `extract-csv --database-dir` path was validated against online
`SELECT` for focused fixtures:

- `DMDUL_NULL2`: offline CSV decoded all 4 rows with NULL combinations. The
  raw metadata bytes `fc 03`, `c0 03`, `3c 00`, and `00 00` matched the
  storage-order two-bit NULL rule, and NULL values were emitted as empty CSV
  fields.
- `DMDUL_DTTM2`: offline CSV decoded 3 rows with packed `DATE`, `TIME`, and
  `TIMESTAMP` values. Online `SELECT` returned the same logical values; offline
  formatting is normalized as `YYYY-MM-DD`, `HH:MI:SS.ffffff`, and
  `YYYY-MM-DD HH:MI:SS.ffffff`.
- `DMDUL_VLEN2`: offline CSV decoded all 8 VARCHAR threshold rows and matched
  online value lengths `1, 2, 10, 127, 128, 255, 256, 1000`.
- `DMDUL_MOD2`: offline CSV skipped the committed deleted row and matched the
  two visible online rows.
- `DMDUL_TYPES3`: offline strict CSV decoded all 4 mixed scalar rows and
  matched online `SELECT` after normalizing display-only differences. Covered
  columns were `TINYINT`, `SMALLINT`, `INT`, `BIGINT`, `REAL`, `FLOAT`,
  `DOUBLE`, `NUMBER(18,4)`, `DECIMAL(18,4)`, `DATE`, `TIME(6)`,
  `TIMESTAMP(6)`, `CHAR(8)`, and `VARCHAR(40)`. The strict report returned
  `strict_ok=true`, `rows_written=4`, and `rows_skipped_decode_error=0`.

The `DMDUL_TYPES3` run also exposed a dictionary-bootstrap hazard. The current
SYSTEM-file heuristic can see noisy `SYSCOLUMNS` candidates with duplicate
`column_id` values, including an unrelated `FINDEXID`-like row. The resolver now
deduplicates by `column_id`, keeps the highest-scoring candidate, and rejects
non-contiguous column ids instead of letting one spurious row shift all later
decoded columns.

Follow-up bootstrap validation preserved `SYSCOLUMNS` `scale` and `nullable`
through `col.dict` and `dump-data`. The generated DUL header for
`DMDUL_TYPES3` now renders numeric and temporal precision from offline
dictionary files: `NUMBER(18,4)`, `DECIMAL(18,4)`, `TIME(6)`, and
`TIMESTAMP(6)`.

Later import validation fixed temporal-with-time-zone DDL precision. When
dictionary scale is a legal SQL fractional precision (`1..6`), generated DDL
now emits forms such as `TIME(6) WITH TIME ZONE` and
`DATETIME(6) WITH TIME ZONE`. Observed non-precision values such as
`TIMESTAMP WITH LOCAL TIME ZONE scale=4102` are not emitted as SQL precision.
Remote `DMDUL_TIME_TYPES` row-archive export, import into DMTEST, and
bidirectional `MINUS` comparison now return `0/0`.

A later owner/schema fix reads `SYSOBJECTS.SCHID` as a 4-byte value and maps the
verified built-in full schema ids. Remote `bootstrap_owner_fix` then emitted
`SYSDBA.DMDUL_TYPES3` instead of the earlier noisy `TEST2.DMDUL_TYPES3`, and
`dump-data --table SYSDBA.DMDUL_TYPES3` generated a DUL header beginning with
`CREATE TABLE SYSDBA.DMDUL_TYPES3`.

This validates the current ordinary-row path for controlled NULL metadata,
fixed temporal payloads, variable-length thresholds, and committed deleted-row
skipping.

## LOB Attachment Export

`dump-data`, `extract-dicts`, and `extract-csv` now default to external LOB
export. The main DUL/CSV file stores a stable placeholder instead of embedding
large LOB values:

```text
@LOB:SYSDBA.T.lob/00000001/DOC.clob
@LOB:SYSDBA.T.lob/00000001/BIN.blob
```

For an output file `SYSDBA.T.dul`, attachments are written under
`SYSDBA.T.lob/` by default:

```text
SYSDBA.T.dul
SYSDBA.T.lob/
  00000001/DOC.clob
  00000001/BIN.blob
  manifest.jsonl
```

The filename row component is the extraction row sequence, not a primary key.
`BLOB` attachments are raw bytes. `CLOB` and `TEXT` attachments are decoded text
written as UTF-8, with the original source encoding recorded in
`manifest.jsonl`. Each manifest row records table, row sequence, column, type,
status, file path, output byte count, and `sha256`. When text transcoding
changes the byte count, `source_bytes` records the original LOB page byte count.

Out-of-line LOB locators now follow the observed DM8 page chain when the shape
matches the controlled evidence. The observed locator is 21 bytes:

```text
00      flag, 0x02 for out-of-line LOB
01..04  LOB id, little-endian
09..12  byte length, little-endian
13..16  group id / tablespace group, little-endian
17..20  first LOB data page, little-endian
```

The observed LOB data pages use page kind `0x20`. The LOB id is stored at page
offset `0x24`, payload length at `0x2c`, and payload bytes start at `0x38`.
The regular page `next_page` pointer links the data pages. The implementation
accepts this narrow shape only: group id, file hint, page number, page kind,
LOB id, and payload bounds must all match. If any check fails, dmdul still
writes the raw locator as `<column>.locator.hex`, emits
`status="unresolved-locator"` in the manifest, and adds the strict error
diagnostic `lob-locator-not-followed`.

Remote validation on 2026-07-03 used `SYSDBA.DMDUL_LOB_INLINE`:

```text
ID|DOC|BIN
1|@LOB:SYSDBA.DMDUL_LOB_INLINE.lob/00000001/DOC.clob|@LOB:SYSDBA.DMDUL_LOB_INLINE.lob/00000001/BIN.blob
```

The generated CLOB attachment contained `CLOB_一`, and the BLOB attachment bytes
were `ca fe ba be`. The `dump-data --dict-dir` report returned
`ok=true`, `strict_ok=true`, and `rows_written=1`.

Remote out-of-line validation on 2026-07-03 used `SYSDBA.DMDUL_LOB_BIG` with a
26000-character CLOB and a 12000-byte BLOB. The row locators were:

```text
DOC: 0260d4060000000000606d00000600000086060000
BIN: 0261d4060000000000e02e0000060000008b060000
```

The DOC byte length field is `0x6d60` (28000 bytes in GB18030), and the first
page is `1670`. The BIN byte length field is `0x2ee0` (12000 bytes), and the
first page is `1675`. Offline `dump-data --dict-dir` wrote:

```text
DOC pages: 1670, 1671, 1672, 1673
BIN pages: 1675, 1676
DOC output: 26000 UTF-8 characters, 30000 bytes
BIN output: 12000 bytes
```

The strict report returned `ok=true`, `strict_ok=true`, and `rows_written=1`.

LOB update / old-version evidence on 2026-07-03 used
`SYSDBA.DMDUL_LOB_UPDATE`:

1. Inserted one row with `OLD_LOB_一_...` CLOB and `cafebabe...` BLOB.
2. Committed.
3. Updated the same row to `NEW_LOB_二_...` CLOB and `deadbeef...` BLOB.
4. Committed.

Online SQL returned only the new version:

```text
DOC_LEN=20000
DOC_PREFIX=NEW_LOB_二_NEW_LOB_二_
BIN_LEN=12000
```

Offline `dump-data --dict-dir` also returned only the new version and reported
`ok=true`, `strict_ok=true`, `rows_written=1`. The attachment verification
showed:

```text
doc_chars=20000
doc_prefix=NEW_LOB_二_NEW_LOB_二_NEW_
contains_old=False
bin_bytes=12000
bin_head=deadbeefdeadbeefdeadbeefdeadbeef
bin_contains_old_cafebabe=False
```

Raw page scanning still found the old LOB payload physically present:

```text
old CLOB pages: 1766 -> 1767 -> 1768, lobid=447586
old BLOB pages: 1769 -> 1770, lobid=447587
new CLOB pages: 1771 -> 1772 -> 1773, lobid=447588
new BLOB pages: 1774 -> 1775, lobid=447589
current table row page: 1776
```

This confirms the extraction rule: do not scan and export arbitrary LOB pages.
For current committed table data, dmdul must decode the active row first, then
follow only the locator stored in that active row. Old LOB page chains can
remain in the data file and must not be treated as current table values.

## Practical Rule Going Forward

For future type work, use paired online/offline evidence:

1. create a deterministic test table;
2. insert boundary values;
3. query both SQL value and `dump(col)`;
4. checkpoint and copy/read the DBF page;
5. correlate row offsets, raw bytes, and decoded output;
6. add a focused regression test before broadening the decoder.
