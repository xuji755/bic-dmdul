# DM8 Type Storage Notes

This note records type-storage evidence captured from the live DM8 test
instance after `select checkpoint(0)` returned `0`.

## Fixture

Calibration table:

```sql
create table SYSDBA.DMDUL_NUM38_STORE (
  ID int primary key,
  N38 number(38),
  D date,
  TS timestamp,
  MARKER varchar(64)
) tablespace DMDUL_TS;
```

Evidence:

- object id: `33713`
- data file: `/dmdata/data/DAMENG/DMDUL_TS01.DBF`
- copied file: `evidence/type_store/DMDUL_TS01.DBF`
- root/data page: `224`
- row offsets: `98`, `158`, `214`, `286`

The row layout observed for this five-column all-non-null table is:

- row length/status: 2 bytes, big-endian length plus status bits
- row metadata: 2 zero bytes for five columns
- fixed area:
  - `ID`: 4 bytes
  - `D`: 3 bytes
  - `TS`: 8 bytes
- variable area:
  - `N38`: variable-length NUMBER payload
  - `MARKER`: variable-length VARCHAR payload

This confirms that rows are not stored by blindly concatenating columns in SQL
column order. Fixed-width and variable-width regions must be decoded separately.

## Row Structure Working Model

Current all-non-null row evidence supports this working structure:

```text
row_head
fixed_area
variable_area
row_tail_or_control
```

It is therefore not correct to model every row as:

```text
row_head, col1_length, col1, col2_length, col2, ...
```

That pattern applies only inside the variable area. Fixed-width columns are
stored first, without a per-column length prefix. Variable-width columns then
follow in column order among the variable-width subset, each with the compact
length prefix already observed for `VARCHAR`.

Observed examples:

### Five-column `DMDUL_NUM38_STORE`

Columns:

```text
ID INT, N38 NUMBER(38), D DATE, TS TIMESTAMP, MARKER VARCHAR(64)
```

Observed row structure:

```text
row_head      4 bytes
fixed_area    ID(4), D(3), TS(8)
variable_area N38(varlen), MARKER(varlen)
tail          19 bytes, not decoded yet
```

Row 1 (`ID=1`, zero number, minimum date marker):

```text
head          00 3c 00 00
ID            01 00 00 00
D             01 80 08
TS            01 80 08 00 00 00 00 00
N38           81 80
MARKER        93 4e 55 4d 33 38 5f 5a 45 52 4f 5f 44 41 54 45 5f 4d 49 4e
tail          01 00 00 00 00 00 ff ff ff ff 7f ff ff b0 42 35 04 00 00
```

### Seventeen-column `DMDUL_TYPE_STORE`

Columns include fixed numeric/date/time fields and variable
`NUMBER/DECIMAL/CHAR/VARCHAR/CLOB/BLOB/MARKER` fields.

Observed row structure:

```text
row_head      7 bytes
fixed_area    ID, TINYINT, SMALLINT, INT, BIGINT, FLOAT, DOUBLE, DATE, TIME, TIMESTAMP
variable_area NUMBER, DECIMAL, CHAR, VARCHAR, CLOB locator, BLOB locator, MARKER
tail          19 bytes, not decoded yet
```

For row 3:

```text
head          01 31 00 00 00 00 00
ID            03 00 00 00
C_TINY        7f
C_SMALL       ff 7f
C_INT         ff ff ff 7f
C_BIG         ff ff ff ff ff ff ff 7f
C_FLOAT       00 00 00 00 00 00 f8 3f
C_DOUBLE      00 00 00 00 00 00 02 40
C_DATE        ea 07 f3
C_TIME        6a 61 00 00 00
C_TS          ea 07 f3 6a 61 e2 f7 13
C_NUM         90 ca 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f 5b
C_DEC         8a c7 0d 23 39 4f 5b 0d 23 39 4f
C_CHAR        83 50 4f 53
C_VC          00 82 <130 bytes of 0x50>
C_CLOB        9e 01 33 a6 06 00 00 00 00 00 11 00 00 00 ...
C_BLOB        91 01 34 a6 06 00 00 00 00 00 04 00 00 00 ca fe ba be
MARKER        8e 54 59 50 45 5f 53 54 4f 52 45 5f 50 4f 53
tail          03 00 00 00 00 00 ff ff ff ff 7f ff ff b9 41 35 04 00 00
```

Open points recorded at this exploration stage before it became the current row
decoder:

- identify exact row-head bits beyond length/delete status;
- decode nonzero row metadata for NULL columns;
- decode the 19-byte row tail/control region;
- confirm whether fixed and variable partitioning changes for nullable,
  updated, compressed, or chained rows;
- decode LOB locator fields rather than treating them as opaque variable values.

Current status: later implementation decodes short inline LOB payloads and
follows the verified 21-byte out-of-line LOB locator through `0x20` LOB data
pages. Unknown locator shapes are preserved as raw locator evidence instead of
being silently decoded.

## Row Slot Directory

The tested BTREE/data pages do have a row indicator list near the page tail.
Entries are 2-byte little-endian page offsets pointing to row heads.

Observed page `224` (`DMDUL_NUM38_STORE`):

```text
row heads: 98, 158, 214, 286
slot bytes at 8174: 1e 01 d6 00 9e 00 62 00
decoded slots: 286, 214, 158, 98
```

Observed page `288` (`DMDUL_TYPE_STORE`):

```text
row heads: 98, 252, 557
slot bytes at 8176: 2d 02 fc 00 62 00
decoded slots: 557, 252, 98
```

The order is currently last physical row back to first physical row. This is
consistent with a slot directory growing from the end of the page toward the
free space. The surrounding slot metadata is not fully decoded yet, so this
should be treated as a row-start pointer list, not yet a complete slot record
format.

For normal page row scanning, this pointer list is the preferred path: use the
slot directory to reach active row heads directly, then use the physical row
chain only as fallback and calibration evidence. In the committed-delete sample
on page `208`, the deleted physical row at offset `135` remains in the row
chain with a high-bit delete flag in its row head, but the page-tail slot list
contains only live row offsets `174` and `98`.

## NUMBER(38)

`NUMBER(38)` is stored as a variable-length base-100 numeric payload.

Observed values:

| Value | Prefix | Payload |
| --- | --- | --- |
| `0` | `81` | `80` |
| `1` | `82` | `c1 02` |
| `12345678901234567890123456789012345678` | `94` | `d3 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f` |
| `-12345678901234567890123456789012345678` | `95` | `2c 59 43 2d 17 0b 59 43 2d 17 0b 59 43 2d 17 0b 59 43 2d 17 66` |

Working decoding rule:

- Payload `80` means zero.
- Positive nonzero numbers:
  - first payload byte is exponent/sign: `0xc1 + base100_exponent`
  - each following byte is one base-100 digit pair plus `1`
  - for the 38-digit positive sample, digit pairs are
    `12 34 56 78 90 ...`; payload bytes are
    `0d 23 39 4f 5b ...`, i.e. each pair plus `1`
- Negative nonzero numbers:
  - first payload byte is `0x3e - base100_exponent`
  - each following byte is `101 - base100_digit_pair`
  - payload ends with terminator `66`

This is equivalent in shape to Oracle-style variable-length base-100 NUMBER
storage. Scale handling for `NUMBER(p,s)` and odd digit counts still needs
additional fixtures before being marked complete.

## DATE

`DATE` is a 3-byte little-endian bit-packed value:

```text
value = year | (month << 15) | (day << 19)
```

Field widths:

- bits `0..14`: year
- bits `15..18`: month, 1-based
- bits `19..23`: day

Observed values:

| SQL value | Raw bytes | Check |
| --- | --- | --- |
| `0001-01-01` | `01 80 08` | `1 | (1 << 15) | (1 << 19)` |
| `2000-01-01` | `d0 87 08` | `2000 | (1 << 15) | (1 << 19)` |
| `2024-02-29` | `e8 07 e9` | `2024 | (2 << 15) | (29 << 19)` |
| `2026-06-30` | `ea 07 f3` | `2026 | (6 << 15) | (30 << 19)` |

## TIMESTAMP

`TIMESTAMP(6)` is 8 bytes:

- bytes `0..2`: the same 3-byte `DATE` encoding
- bytes `3..7`: 5-byte little-endian time value

The time value packs:

```text
time_value = hour | (minute << 5) | (second << 11) | (microsecond << 17)
```

Field widths:

- bits `0..4`: hour
- bits `5..10`: minute
- bits `11..16`: second
- bits `17..36`: microsecond

Observed values:

| SQL value | Raw bytes |
| --- | --- |
| `0001-01-01 00:00:00.000000` | `01 80 08 00 00 00 00 00` |
| `2000-01-01 00:00:00.000000` | `d0 87 08 00 00 00 00 00` |
| `2024-02-29 23:59:59.123456` | `e8 07 e9 77 df 81 c4 03` |
| `2026-06-30 10:11:12.654321` | `ea 07 f3 6a 61 e2 f7 13` |

For example:

```text
23 | (59 << 5) | (59 << 11) | (123456 << 17)
  = 0x03c481df77
  = bytes 77 df 81 c4 03
```

Timezone-bearing timestamp variants are not covered by this evidence.

## Online `dump()` Calibration: `SYSDBA.DMDUL_DUMP_TYPES`

Captured with `dump(col)` on the live DM8 test instance after creating
`SYSDBA.DMDUL_DUMP_TYPES` and running `select checkpoint(0)`. The raw disql
output is stored at `tmp/dmdul_dump_types.out`. This online evidence is useful
for mapping SQL values to DM's reported internal type code and byte sequence;
the DBF page layout can still differ in how row fixed/variable areas arrange
those bytes.

Fixture columns:

```text
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
```

Observed `dump()` type codes and representative payloads:

| SQL type | dump type | dump length | Observed bytes / meaning |
| --- | ---: | ---: | --- |
| TINYINT | 7 | 4 | `1` => `1,0,0,0`; `-128` => `128,255,255,255`; DM reports integer family as 4-byte little-endian in `dump()` |
| SMALLINT | 7 | 4 | `2` => `2,0,0,0`; `-32768` => `0,128,255,255`; `32767` => `255,127,0,0` |
| INT / INTEGER | 7 | 4 | little-endian signed 32-bit, e.g. `2147483647` => `255,255,255,127` |
| BIGINT | 8 | 8 | little-endian signed 64-bit, e.g. `5` => `5,0,0,0,0,0,0,0` |
| REAL | 10 | 4 | IEEE float little-endian, e.g. `1.25` => `0,0,160,63` |
| FLOAT | 11 | 8 | IEEE double little-endian in this fixture, e.g. `1.5` => `0,0,0,0,0,0,248,63` |
| DOUBLE | 11 | 8 | IEEE double little-endian, e.g. `2.25` => `0,0,0,0,0,0,2,64` |
| NUMBER(38) | 9 | 1/20/21 | `0` => `128`; positive 38-digit integer begins `211,13,35,...`; negative form begins `44,...` and ends `102` |
| NUMERIC(30,10) | 9 | 1/16/17 | same base-100 family as NUMBER, with scale preserved by exponent/byte count |
| DECIMAL(18,4) | 9 | 1/10/11 | same base-100 family as NUMBER |
| DATE | 14 | 13 | `2026-06-30` => `234,7,6,30,0,0,0,0,0,0,232,3,0`; `dump()` expands DATE to 13 bytes |
| TIME(6) | 15 | 13 | `10:11:12.654321` => `108,7,1,1,10,11,12,104,37,0,232,3,39`; includes default date-like fields in `dump()` |
| TIMESTAMP(6) | 16 | 13 | `2026-06-30 10:11:12.654321` => `234,7,6,30,10,11,12,104,37,0,232,3,39` |
| DATETIME(6) | 16 | 13 | same dump code/shape as TIMESTAMP(6) |
| CHAR(8) | 2 | 8 | padded bytes, e.g. `CH_A` => `67,72,95,65,32,32,32,32` |
| VARCHAR(40) | 2 | value length | raw text bytes, e.g. `VARCHAR_A` => `86,65,82,67,72,65,82,95,65` |
| CLOB | 19 | 64 | inline/locator structure; short text appears at the tail of the 64-byte payload in this fixture |
| BLOB | 12 | 55 | inline/locator structure; short binary bytes appear at the tail of the 55-byte payload in this fixture |

Important interpretation points:

- `dump()` reports DM's logical/internal value representation, not necessarily
the exact row-page byte layout. Prior DBF evidence shows page rows store DATE as
3 bytes, TIME as 5 bytes, and TIMESTAMP/DATETIME as 8 bytes in ordinary table
rows; `dump()` expands these to 13 bytes.
- Integer `dump()` confirms little-endian signed numeric storage for the integer
family. Page-row decoding should continue to use dictionary `DATA_LENGTH` or
observed fixed-width layout to decide whether a column consumes 1/2/4/8 bytes in
DBF rows.
- NUMBER/NUMERIC/DECIMAL use the same variable-length base-100 family. Positive
numbers use a high first byte, zero is `0x80`, and negative numbers use a low
first byte with `0x66` terminator in the observed evidence.
- CLOB/BLOB values in this short-row fixture are not plain text/raw bytes only;
they include a locator/control prefix. The early decoder treated unresolved LOB
payloads as raw hex. Current `dump-data` removes verified short inline LOB
prefixes and follows verified out-of-line LOB page chains; unresolved locator
shapes are still preserved rather than guessed.

Dictionary metadata for the fixture confirmed official type names and lengths:

```text
NUMBER(38)       DATA_LENGTH=22, DATA_PRECISION=38, DATA_SCALE=0
NUMERIC(30,10)   DATA_LENGTH=22, DATA_PRECISION=30, DATA_SCALE=10
DECIMAL(18,4)    DATA_LENGTH=22, DATA_PRECISION=18, DATA_SCALE=4
DATE             DATA_LENGTH=3
TIME(6)          DATA_LENGTH=5, DATA_SCALE=6
TIMESTAMP(6)     DATA_LENGTH=8, DATA_SCALE=6
DATETIME(6)      DATA_LENGTH=8, DATA_SCALE=6
CLOB/BLOB        DATA_LENGTH=2147483647
```
