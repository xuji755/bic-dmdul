from __future__ import annotations

from .metadata import ColumnMeta
from .row import ObservedRow, decode_observed_var_length


class DecodeError(ValueError):
    pass


def decode_observed_row_values(
    row: ObservedRow,
    columns: tuple[ColumnMeta, ...],
) -> list[object]:
    """Decode a narrow subset of observed ordinary row-table records.

    Current support is intentionally limited to non-NULL scalar rows used by
    the controlled extractor tests. NULL bitmap decoding is still under
    exploration.
    """

    offset = _observed_column_start_offset(columns)
    values: list[object] = []
    data = row.data
    for column in columns:
        type_name = column.type_name.upper()
        if type_name == "INT":
            _require(data, offset, 4, column.name)
            values.append(int.from_bytes(data[offset : offset + 4], "little", signed=True))
            offset += 4
        elif type_name == "BIGINT":
            _require(data, offset, 8, column.name)
            values.append(int.from_bytes(data[offset : offset + 8], "little", signed=True))
            offset += 8
        elif type_name in {"VARCHAR", "CHAR"}:
            decoded = decode_observed_var_length(data[offset:])
            offset += decoded.encoded_size
            _require(data, offset, decoded.length, column.name)
            raw = data[offset : offset + decoded.length]
            text = raw.decode("utf-8", errors="replace")
            values.append(text.rstrip(" ") if type_name == "CHAR" else text)
            offset += decoded.length
        else:
            raise DecodeError(f"unsupported column type for observed decoder: {type_name}")
    return values


def _observed_column_start_offset(columns: tuple[ColumnMeta, ...]) -> int:
    # Controlled samples show one prefix byte after the row length for <=4
    # columns and two bytes for 5 columns. This is likely NULL metadata and will
    # be replaced once the bitmap/directory is fully decoded.
    return 4 if len(columns) >= 5 else 3


def _require(data: bytes, offset: int, length: int, column_name: str) -> None:
    if offset + length > len(data):
        raise DecodeError(
            f"row is too short while decoding column {column_name}: "
            f"offset={offset}, length={length}, row_length={len(data)}"
        )
