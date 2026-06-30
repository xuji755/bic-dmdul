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
