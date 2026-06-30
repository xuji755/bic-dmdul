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

Open points before this becomes a complete row decoder:

- identify exact row-head bits beyond length/delete status;
- decode nonzero row metadata for NULL columns;
- decode the 19-byte row tail/control region;
- confirm whether fixed and variable partitioning changes for nullable,
  updated, compressed, or chained rows;
- decode LOB locator fields rather than treating them as opaque variable values.

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
