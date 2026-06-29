from __future__ import annotations

import struct

from .metadata import ColumnMeta
from .row import ObservedRow, decode_observed_var_length


class DecodeError(ValueError):
    def __init__(self, message: str, *, code: str = "row-decode-error") -> None:
        super().__init__(message)
        self.code = code


SUPPORTED_OBSERVED_TYPE_NAMES = frozenset(
    {
        "BIGINT",
        "CHAR",
        "DOUBLE",
        "FLOAT",
        "INT",
        "REAL",
        "VARCHAR",
    }
)


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
    _require_supported_row_metadata(row.data, offset)
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
        elif type_name in {"DOUBLE", "REAL"}:
            _require(data, offset, 8, column.name)
            values.append(struct.unpack("<d", data[offset : offset + 8])[0])
            offset += 8
        elif type_name == "FLOAT":
            if column.length == 4:
                _require(data, offset, 4, column.name)
                values.append(struct.unpack("<f", data[offset : offset + 4])[0])
                offset += 4
            else:
                _require(data, offset, 8, column.name)
                values.append(struct.unpack("<d", data[offset : offset + 8])[0])
                offset += 8
        elif type_name in {"VARCHAR", "CHAR"}:
            try:
                decoded = decode_observed_var_length(data[offset:])
            except ValueError as exc:
                raise DecodeError(
                    f"invalid variable-length prefix for column {column.name}: {exc}"
                ) from exc
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


def _require_supported_row_metadata(data: bytes, column_start_offset: int) -> None:
    if len(data) < column_start_offset:
        raise DecodeError(
            "row is too short while decoding row metadata: "
            f"metadata_end={column_start_offset}, row_length={len(data)}"
        )
    metadata = data[2:column_start_offset]
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
