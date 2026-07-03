from __future__ import annotations

import struct
from dataclasses import dataclass

from .metadata import ColumnMeta
from .row import ObservedRow, decode_observed_var_length, describe_observed_row_layout


class DecodeError(ValueError):
    def __init__(self, message: str, *, code: str = "row-decode-error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LobValue:
    type_name: str
    raw: bytes
    inline_payload: bytes | None
    text: str | None = None
    source_encoding: str | None = None

    @property
    def is_inline(self) -> bool:
        return self.inline_payload is not None


SUPPORTED_OBSERVED_TYPE_NAMES = frozenset(
    {
        "BIGINT",
        "BINARY",
        "BLOB",
        "BYTE",
        "CHAR",
        "CLOB",
        "DATETIME WITH TIME ZONE",
        "DATE",
        "DATETIME",
        "DEC",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "INT",
        "INTEGER",
        "INTERVAL DAY TO SECOND",
        "NUMBER",
        "NUMERIC",
        "REAL",
        "ROWID",
        "SMALLINT",
        "TEXT",
        "TIME",
        "TIME WITH TIME ZONE",
        "TIMESTAMP",
        "TIMESTAMP WITH LOCAL TIME ZONE",
        "TIMESTAMP WITH TIME ZONE",
        "TINYINT",
        "VARBINARY",
        "VARCHAR",
        "VARCHAR2",
    }
)


def decode_observed_row_values(
    row: ObservedRow,
    columns: tuple[ColumnMeta, ...],
    *,
    external_lobs: bool = False,
) -> list[object]:
    """Decode observed ordinary row-table records.

    Current all-non-null evidence shows DM stores fixed-width columns first,
    followed by variable-width columns, while result values must still be
    returned in SQL column order. Nullable samples show the compact metadata
    bytes encode two bits per storage-order column: `00` for present and `11`
    for NULL. Fixed-width NULL columns still reserve their fixed bytes; variable
    NULL columns omit their variable-length prefix and payload.
    """

    try:
        layout = describe_observed_row_layout(row, column_count=len(columns))
    except ValueError as exc:
        raise DecodeError(str(exc)) from exc
    values: list[object | None] = [None] * len(columns)
    data = row.data
    offset = layout.column_payload_offset

    fixed_columns: list[tuple[int, ColumnMeta]] = []
    variable_columns: list[tuple[int, ColumnMeta]] = []
    for index, column in enumerate(columns):
        if _fixed_width(column) is None:
            variable_columns.append((index, column))
        else:
            fixed_columns.append((index, column))

    storage_columns = fixed_columns + variable_columns
    nulls_by_index = _decode_null_metadata(layout.metadata, storage_columns)

    for index, column in fixed_columns:
        consumed = _fixed_width(column)
        if consumed is None:
            raise DecodeError(f"column is not fixed-width: {column.type_name.upper()}")
        if nulls_by_index[index]:
            _require(data, offset, consumed, column.name)
            values[index] = None
        else:
            values[index], consumed = _decode_fixed_value(data, offset, column)
        offset += consumed
    for index, column in variable_columns:
        if nulls_by_index[index]:
            values[index] = None
            continue
        values[index], consumed = _decode_variable_value(
            data,
            offset,
            column,
            external_lobs=external_lobs,
        )
        offset += consumed

    return list(values)


def _decode_fixed_value(data: bytes, offset: int, column: ColumnMeta) -> tuple[object, int]:
    type_name = column.type_name.upper()
    length = _fixed_width(column)
    if length is None:
        raise DecodeError(f"column is not fixed-width: {type_name}")
    _require(data, offset, length, column.name)
    raw = data[offset : offset + length]
    if type_name == "TINYINT":
        return int.from_bytes(raw, "little", signed=True), length
    if type_name == "SMALLINT":
        return int.from_bytes(raw, "little", signed=True), length
    if type_name in {"INT", "INTEGER"}:
        return int.from_bytes(raw, "little", signed=True), length
    if type_name == "BIGINT":
        return int.from_bytes(raw, "little", signed=True), length
    if type_name == "REAL":
        return struct.unpack("<f", raw)[0], length
    if type_name == "FLOAT" and length == 4:
        return struct.unpack("<f", raw)[0], length
    if type_name in {"FLOAT", "DOUBLE"}:
        return struct.unpack("<d", raw)[0], length
    if type_name == "DATE":
        return _decode_date(raw), length
    if type_name == "TIME":
        return _decode_time(raw), length
    if type_name in {"TIMESTAMP", "DATETIME", "TIMESTAMP WITH LOCAL TIME ZONE"}:
        return _decode_timestamp(raw), length
    if type_name == "TIME WITH TIME ZONE":
        return _decode_time_with_timezone(raw), length
    if type_name in {"TIMESTAMP WITH TIME ZONE", "DATETIME WITH TIME ZONE"}:
        return _decode_timestamp_with_timezone(raw), length
    if type_name == "BYTE":
        return raw.hex(), length
    if type_name == "CHAR":
        return _decode_character_bytes(raw).rstrip(" "), length
    if type_name == "ROWID":
        return _decode_rowid(raw), length
    if type_name == "INTERVAL DAY TO SECOND":
        return _decode_interval_day_to_second(raw), length
    raise DecodeError(f"unsupported fixed-width column type for observed decoder: {type_name}")


def _decode_variable_value(
    data: bytes,
    offset: int,
    column: ColumnMeta,
    *,
    external_lobs: bool = False,
) -> tuple[object, int]:
    type_name = column.type_name.upper()
    if type_name in {"NUMBER", "DEC", "DECIMAL", "NUMERIC"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        return _decode_number(raw), consumed
    if type_name in {"BINARY", "VARBINARY"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        return raw.hex(), consumed
    if type_name == "BLOB":
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        inline_payload = _inline_lob_payload(raw)
        if external_lobs:
            return LobValue(type_name=type_name, raw=raw, inline_payload=inline_payload), consumed
        return (inline_payload if inline_payload is not None else raw).hex(), consumed
    if type_name == "CLOB":
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        inline_payload = _inline_lob_payload(raw)
        if inline_payload is None:
            if external_lobs:
                return LobValue(type_name=type_name, raw=raw, inline_payload=None), consumed
            return raw.hex(), consumed
        text, encoding = _decode_character_bytes_with_encoding(inline_payload)
        if external_lobs:
            return (
                LobValue(
                    type_name=type_name,
                    raw=raw,
                    inline_payload=inline_payload,
                    text=text,
                    source_encoding=encoding,
                ),
                consumed,
            )
        return text, consumed
    if type_name == "TEXT":
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        inline_payload = _inline_lob_payload(raw)
        payload = inline_payload if inline_payload is not None else raw
        text, encoding = _decode_character_bytes_with_encoding(payload)
        if external_lobs:
            return (
                LobValue(
                    type_name=type_name,
                    raw=raw,
                    inline_payload=payload,
                    text=text,
                    source_encoding=encoding,
                ),
                consumed,
            )
        return text, consumed
    if type_name in {"VARCHAR", "VARCHAR2", "CHAR"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        text = _decode_character_bytes(raw)
        return (text.rstrip(" ") if type_name == "CHAR" else text), consumed
    raise DecodeError(f"unsupported variable-width column type for observed decoder: {type_name}")


def _fixed_width(column: ColumnMeta) -> int | None:
    type_name = column.type_name.upper()
    if type_name == "TINYINT":
        return 1
    if type_name == "SMALLINT":
        return 2
    if type_name in {"INT", "INTEGER"}:
        return 4
    if type_name == "BIGINT":
        return 8
    if type_name == "REAL":
        return 4
    if type_name == "FLOAT":
        return 4 if column.length == 4 else 8
    if type_name == "DOUBLE":
        return 8
    if type_name == "DATE":
        return 3
    if type_name == "TIME":
        return 5
    if type_name in {"TIMESTAMP", "DATETIME", "TIMESTAMP WITH LOCAL TIME ZONE"}:
        return 8
    if type_name == "TIME WITH TIME ZONE":
        return 7
    if type_name in {"TIMESTAMP WITH TIME ZONE", "DATETIME WITH TIME ZONE"}:
        return 10
    if type_name == "BYTE":
        return 1
    if type_name == "CHAR" and column.length is not None and 0 < column.length <= 2:
        return column.length
    if type_name == "ROWID":
        return 12
    if type_name == "INTERVAL DAY TO SECOND":
        return 24
    return None

def _read_observed_var_bytes(data: bytes, column_name: str) -> tuple[bytes, int]:
    try:
        decoded = decode_observed_var_length(data)
    except ValueError as exc:
        raise DecodeError(
            f"invalid variable-length prefix for column {column_name}: {exc}"
        ) from exc
    offset = decoded.encoded_size
    _require(data, offset, decoded.length, column_name)
    return data[offset : offset + decoded.length], decoded.encoded_size + decoded.length


def _decode_date(raw: bytes) -> str:
    value = int.from_bytes(raw, "little")
    year = value & 0x7FFF
    month = (value >> 15) & 0x0F
    day = (value >> 19) & 0x1F
    return f"{year:04d}-{month:02d}-{day:02d}"


def _decode_time(raw: bytes) -> str:
    value = int.from_bytes(raw, "little")
    hour = value & 0x1F
    minute = (value >> 5) & 0x3F
    second = (value >> 11) & 0x3F
    microsecond = (value >> 17) & 0xFFFFF
    return f"{hour:02d}:{minute:02d}:{second:02d}.{microsecond:06d}"


def _decode_timestamp(raw: bytes) -> str:
    return f"{_decode_date(raw[:3])} {_decode_time(raw[3:8])}"


def _decode_time_with_timezone(raw: bytes) -> str:
    return f"{_decode_time(raw[:5])} {_decode_timezone_offset(raw[5:7])}"


def _decode_timestamp_with_timezone(raw: bytes) -> str:
    return f"{_decode_timestamp(raw[:8])} {_decode_timezone_offset(raw[8:10])}"


def _decode_timezone_offset(raw: bytes) -> str:
    minutes = int.from_bytes(raw, "little", signed=True)
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _decode_interval_day_to_second(raw: bytes) -> str:
    if len(raw) != 24:
        raise DecodeError(f"invalid INTERVAL DAY TO SECOND payload length: {len(raw)}")
    day, hour, minute, second, microsecond, _metadata = struct.unpack("<iiiiii", raw)
    components = (day, hour, minute, second, microsecond)
    negative = any(value < 0 for value in components)
    absolute = tuple(abs(value) for value in components)
    sign = "-" if negative else ""
    return (
        f"{sign}{absolute[0]} "
        f"{absolute[1]:02d}:{absolute[2]:02d}:{absolute[3]:02d}.{absolute[4]:06d}"
    )


def _decode_rowid(raw: bytes) -> str:
    if len(raw) != 12:
        raise DecodeError(f"invalid ROWID payload length: {len(raw)}")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    parts: list[str] = []
    for offset in range(0, len(raw), 4):
        value = int.from_bytes(raw[offset : offset + 4], "big", signed=False)
        parts.extend(alphabet[(value >> shift) & 0x3F] for shift in (30, 24, 18, 12, 6, 0))
    return "".join(parts)


def _inline_lob_payload(raw: bytes) -> bytes | None:
    if len(raw) < 13:
        return None
    if raw[0] != 0x01:
        return None
    inline_length = int.from_bytes(raw[9:13], "little", signed=False)
    if inline_length != len(raw) - 13:
        return None
    return raw[13:]


def decode_character_bytes_with_encoding(raw: bytes) -> tuple[str, str]:
    return _decode_character_bytes_with_encoding(raw)


def _decode_character_bytes_with_encoding(raw: bytes) -> tuple[str, str]:
    if all(byte < 0x80 for byte in raw):
        return raw.decode("ascii"), "ascii"
    try:
        return raw.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _decode_character_bytes(raw: bytes) -> str:
    return decode_character_bytes_with_encoding(raw)[0]


def _decode_number(raw: bytes) -> str:
    if raw == b"\x80":
        return "0"
    if not raw:
        raise DecodeError("empty NUMBER payload")
    if raw[0] >= 0x80:
        return _decode_positive_number(raw)
    return _decode_negative_number(raw)


def _decode_positive_number(raw: bytes) -> str:
    exponent = raw[0] - 0xC1
    pairs = [byte - 1 for byte in raw[1:]]
    return _decimal_from_base100(sign=1, exponent=exponent, pairs=pairs)


def _decode_negative_number(raw: bytes) -> str:
    payload = raw[1:-1] if raw.endswith(b"\x66") else raw[1:]
    exponent = 0x3E - raw[0]
    pairs = [101 - byte for byte in payload]
    return _decimal_from_base100(sign=-1, exponent=exponent, pairs=pairs)


def _decimal_from_base100(*, sign: int, exponent: int, pairs: list[int]) -> str:
    if not pairs:
        return "0"
    if any(pair < 0 or pair > 99 for pair in pairs):
        raise DecodeError(f"invalid NUMBER base-100 digit sequence: {pairs!r}")

    digits = "".join(f"{pair:02d}" for pair in pairs)
    point = (exponent + 1) * 2
    if point <= 0:
        text = "0." + ("0" * -point) + digits
    elif point >= len(digits):
        text = digits + ("0" * (point - len(digits)))
    else:
        text = digits[:point] + "." + digits[point:]

    integer, dot, fraction = text.partition(".")
    integer = integer.lstrip("0") or "0"
    if dot:
        fraction = fraction.rstrip("0")
        text = integer if not fraction else f"{integer}.{fraction}"
    else:
        text = integer
    if sign < 0 and text != "0":
        text = "-" + text
    return text


def _decode_null_metadata(
    metadata: bytes,
    storage_columns: list[tuple[int, ColumnMeta]],
) -> dict[int, bool]:
    value = int.from_bytes(metadata, "little")
    nulls_by_index: dict[int, bool] = {}
    for storage_position, (index, column) in enumerate(storage_columns):
        state = (value >> (storage_position * 2)) & 0x03
        if state == 0:
            nulls_by_index[index] = False
        elif state == 3:
            nulls_by_index[index] = True
        else:
            raise DecodeError(
                "unsupported row metadata state before column payload; possible "
                f"column directory or transaction flags are not decoded yet: "
                f"column={column.name}, storage_position={storage_position}, "
                f"state={state}",
                code="unsupported-row-metadata",
            )
    extra_bits = value >> (len(storage_columns) * 2)
    if extra_bits:
        raise DecodeError(
            "unsupported row metadata bits beyond decoded NULL bitmap: "
            f"metadata={metadata.hex()}",
            code="unsupported-row-metadata",
        )
    return nulls_by_index


def _require(data: bytes, offset: int, length: int, column_name: str) -> None:
    if offset + length > len(data):
        raise DecodeError(
            f"row is too short while decoding column {column_name}: "
            f"offset={offset}, length={length}, row_length={len(data)}"
        )
