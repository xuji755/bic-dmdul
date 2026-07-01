from __future__ import annotations

import struct

from .metadata import ColumnMeta
from .row import ObservedRow, decode_observed_var_length, describe_observed_row_layout


class DecodeError(ValueError):
    def __init__(self, message: str, *, code: str = "row-decode-error") -> None:
        super().__init__(message)
        self.code = code


SUPPORTED_OBSERVED_TYPE_NAMES = frozenset(
    {
        "BIGINT",
        "BLOB",
        "CHAR",
        "CLOB",
        "DATE",
        "DATETIME",
        "DECIMAL",
        "DOUBLE",
        "FLOAT",
        "INT",
        "INTEGER",
        "NUMBER",
        "NUMERIC",
        "REAL",
        "SMALLINT",
        "TIME",
        "TIMESTAMP",
        "TINYINT",
        "VARCHAR",
    }
)


def decode_observed_row_values(
    row: ObservedRow,
    columns: tuple[ColumnMeta, ...],
) -> list[object]:
    """Decode observed ordinary row-table records.

    Current all-non-null evidence shows DM stores fixed-width columns first,
    followed by variable-width columns, while result values must still be
    returned in SQL column order.
    """

    try:
        layout = describe_observed_row_layout(row, column_count=len(columns))
    except ValueError as exc:
        raise DecodeError(str(exc)) from exc
    _require_supported_row_metadata(layout.metadata)
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

    for index, column in fixed_columns:
        value, consumed = _decode_fixed_value(data, offset, column)
        values[index] = value
        offset += consumed
    for index, column in variable_columns:
        value, consumed = _decode_variable_value(data, offset, column)
        values[index] = value
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
    if type_name in {"TIMESTAMP", "DATETIME"}:
        return _decode_timestamp(raw), length
    raise DecodeError(f"unsupported fixed-width column type for observed decoder: {type_name}")


def _decode_variable_value(data: bytes, offset: int, column: ColumnMeta) -> tuple[object, int]:
    type_name = column.type_name.upper()
    if type_name in {"NUMBER", "DECIMAL", "NUMERIC"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        return _decode_number(raw), consumed
    if type_name in {"CLOB", "BLOB"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        return raw.hex(), consumed
    if type_name in {"VARCHAR", "CHAR"}:
        raw, consumed = _read_observed_var_bytes(data[offset:], column.name)
        text = raw.decode("utf-8", errors="replace")
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
    if type_name in {"TIMESTAMP", "DATETIME"}:
        return 8
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


def _require_supported_row_metadata(metadata: bytes) -> None:
    if any(metadata):
        raise DecodeError(
            "unsupported row metadata before column payload; possible NULL bitmap, "
            "column directory, or transaction flags are not decoded yet",
            code="unsupported-row-metadata",
        )


def _require(data: bytes, offset: int, length: int, column_name: str) -> None:
    if offset + length > len(data):
        raise DecodeError(
            f"row is too short while decoding column {column_name}: "
            f"offset={offset}, length={length}, row_length={len(data)}"
        )
