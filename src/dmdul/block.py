from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .decode import DecodeError, decode_observed_row_values
from .metadata import ColumnMeta
from .page import ObservedPageHeader
from .row import (
    ObservedRow,
    decode_observed_var_length,
    describe_observed_row_layout,
    scan_observed_row_chain,
)
from .storage import DataFile


FIXED_TRACE_LENGTHS = {
    "INT": 4,
    "INTEGER": 4,
    "BIGINT": 8,
    "DOUBLE": 8,
    "REAL": 8,
    "FLOAT": None,
    "DATE": 3,
    "TIME": 5,
    "TIMESTAMP": 8,
}

VARIABLE_TRACE_TYPES = {
    "CHAR",
    "VARCHAR",
    "VARCHAR2",
}

LOB_TRACE_TYPES = {
    "BLOB",
    "CLOB",
}


def analyze_data_block(
    *,
    page: bytes,
    page_no: int | None = None,
    object_id: int | None = None,
    columns: tuple[ColumnMeta, ...] = (),
    row_start_offset: int = 0x62,
    max_rows: int = 128,
    candidate_scan_bytes: int = 512,
) -> dict[str, Any]:
    header = ObservedPageHeader.from_page(page)
    rows = scan_observed_row_chain(
        page,
        start_offset=row_start_offset,
        max_rows=max_rows,
    )
    return {
        "mode": "dm-data-block-analysis",
        "page_no": page_no,
        "page_header": header.as_dict(),
        "page_type_candidates": _page_type_candidates(header),
        "object_id": object_id,
        "object_id_candidates": _object_id_candidates(
            page,
            object_id=object_id,
            scan_bytes=candidate_scan_bytes,
        ),
        "row_start_offset": row_start_offset,
        "rows_total": len(rows),
        "rows": [
            _row_analysis(row, columns=columns, row_index=index)
            for index, row in enumerate(rows)
        ],
    }


def analyze_data_file_block(
    *,
    path: Path,
    page_no: int,
    page_size: int = 8192,
    object_id: int | None = None,
    columns: tuple[ColumnMeta, ...] = (),
    row_start_offset: int = 0x62,
    max_rows: int = 128,
    candidate_scan_bytes: int = 512,
) -> dict[str, Any]:
    data_file = DataFile(path, page_size=page_size)
    return analyze_data_block(
        page=data_file.read_page(page_no),
        page_no=page_no,
        object_id=object_id,
        columns=columns,
        row_start_offset=row_start_offset,
        max_rows=max_rows,
        candidate_scan_bytes=candidate_scan_bytes,
    )


def load_column_meta_from_jsonl(path: Path) -> tuple[ColumnMeta, ...]:
    columns: list[ColumnMeta] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        columns.append(_column_meta_from_mapping(payload))
    return tuple(columns)


def parse_column_specs(specs: tuple[str, ...]) -> tuple[ColumnMeta, ...]:
    columns: list[ColumnMeta] = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError(f"invalid column spec: {spec}")
        length = int(parts[2]) if len(parts) == 3 and parts[2] else None
        columns.append(
            ColumnMeta(
                name=parts[0],
                type_name=parts[1],
                length=length,
            )
        )
    return tuple(columns)


def _column_meta_from_mapping(payload: dict[str, Any]) -> ColumnMeta:
    name = payload.get("name") or payload.get("column_name")
    type_name = payload.get("type_name") or payload.get("type")
    if name is None or type_name is None:
        raise ValueError(f"column row is missing name/type_name: {payload}")
    raw_length = payload.get("length")
    return ColumnMeta(
        name=str(name),
        type_name=str(type_name),
        length=int(raw_length) if raw_length is not None else None,
    )


def _page_type_candidates(header: ObservedPageHeader) -> list[dict[str, Any]]:
    return [
        {
            "name": "first-byte-page-type",
            "offset": 0,
            "size": 1,
            "value": header.page_type_raw,
            "hex": f"0x{header.page_type_raw:02x}",
            "status": "observed-candidate",
        },
        {
            "name": "page-kind-field",
            "offset": 20,
            "size": 4,
            "value": header.page_kind_raw,
            "hex": f"0x{header.page_kind_raw:08x}",
            "label": header.page_kind_label,
            "status": "observed-candidate",
        },
    ]


def _object_id_candidates(
    page: bytes,
    *,
    object_id: int | None,
    scan_bytes: int,
) -> list[dict[str, Any]]:
    if object_id is None:
        return []
    marker = object_id.to_bytes(4, "little", signed=False)
    limit = min(len(page), max(0, scan_bytes))
    candidates: list[dict[str, Any]] = []
    start = 0
    while True:
        offset = page.find(marker, start, limit)
        if offset < 0:
            break
        candidates.append(
            {
                "offset": offset,
                "size": 4,
                "value": object_id,
                "hex": page[offset : offset + 4].hex(),
                "status": "exact-u32le-match",
            }
        )
        start = offset + 1
    return candidates


def _row_analysis(
    row: ObservedRow,
    *,
    columns: tuple[ColumnMeta, ...],
    row_index: int,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "row_index": row_index,
        "page_offset": row.page_offset,
        "length": row.length,
        "raw_len_flags": row.header.raw_len_flags,
        "raw_len_flags_hex": f"0x{row.header.raw_len_flags:04x}",
        "deleted": row.is_deleted,
    }
    if columns:
        try:
            layout = describe_observed_row_layout(row, column_count=len(columns))
            item["layout"] = {
                "header_size": layout.header_size,
                "metadata_hex": layout.metadata.hex(),
                "metadata_size": layout.metadata_size,
                "column_payload_offset": layout.column_payload_offset,
                "has_unsupported_metadata": layout.has_unsupported_metadata,
            }
            item["field_trace"] = _field_trace(
                row,
                columns=columns,
                payload_offset=layout.column_payload_offset,
            )
            try:
                item["decoded_values"] = decode_observed_row_values(row, columns)
                item["decode_status"] = "ok"
            except DecodeError as exc:
                item["decode_status"] = exc.code
                item["decode_error"] = str(exc)
        except ValueError as exc:
            item["decode_status"] = "row-layout-error"
            item["decode_error"] = str(exc)
    return item


def _field_trace(
    row: ObservedRow,
    *,
    columns: tuple[ColumnMeta, ...],
    payload_offset: int,
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    relative_offset = payload_offset
    for column in columns:
        entry = _field_trace_entry(row, column, relative_offset=relative_offset)
        trace.append(entry)
        consumed = entry.get("consumed_bytes")
        if isinstance(consumed, int):
            relative_offset += consumed
    return trace


def _field_trace_entry(
    row: ObservedRow,
    column: ColumnMeta,
    *,
    relative_offset: int,
) -> dict[str, Any]:
    type_name = column.type_name.upper()
    entry: dict[str, Any] = {
        "name": column.name,
        "type_name": type_name,
        "relative_offset": relative_offset,
        "page_offset": row.page_offset + relative_offset,
    }
    data = row.data
    if relative_offset >= len(data):
        entry.update({"status": "past-row-end", "consumed_bytes": None})
        return entry
    if type_name in VARIABLE_TRACE_TYPES:
        return _variable_field_trace(entry, data, relative_offset)
    if type_name in LOB_TRACE_TYPES:
        return _raw_field_trace(
            entry,
            data,
            relative_offset=relative_offset,
            length=column.length,
            status="lob-locator-not-decoded",
        )
    length = _fixed_trace_length(type_name, column.length)
    if length is not None:
        return _raw_field_trace(
            entry,
            data,
            relative_offset=relative_offset,
            length=length,
            status="fixed-width-trace",
        )
    return _raw_field_trace(
        entry,
        data,
        relative_offset=relative_offset,
        length=column.length,
        status="type-storage-not-decoded",
    )


def _variable_field_trace(
    entry: dict[str, Any],
    data: bytes,
    relative_offset: int,
) -> dict[str, Any]:
    try:
        decoded = decode_observed_var_length(data[relative_offset:])
    except ValueError as exc:
        entry.update({"status": "invalid-var-length-prefix", "error": str(exc)})
        return entry
    start = relative_offset + decoded.encoded_size
    stop = start + decoded.length
    entry.update(
        {
            "status": "variable-length-trace",
            "length": decoded.length,
            "length_prefix_size": decoded.encoded_size,
            "raw_hex": data[start:stop].hex(),
            "text": data[start:stop].decode("utf-8", errors="replace"),
            "consumed_bytes": decoded.encoded_size + decoded.length,
        }
    )
    if stop > len(data):
        entry["status"] = "variable-field-past-row-end"
    return entry


def _raw_field_trace(
    entry: dict[str, Any],
    data: bytes,
    *,
    relative_offset: int,
    length: int | None,
    status: str,
) -> dict[str, Any]:
    if length is None:
        sample = data[relative_offset : min(len(data), relative_offset + 16)]
        entry.update(
            {
                "status": status,
                "raw_sample_hex": sample.hex(),
                "consumed_bytes": None,
            }
        )
        return entry
    stop = relative_offset + length
    raw = data[relative_offset:stop]
    entry.update(
        {
            "status": status if stop <= len(data) else "fixed-field-past-row-end",
            "length": length,
            "raw_hex": raw.hex(),
            "consumed_bytes": length if stop <= len(data) else None,
        }
    )
    return entry


def _fixed_trace_length(type_name: str, column_length: int | None) -> int | None:
    if type_name == "FLOAT":
        return 4 if column_length == 4 else 8
    if type_name in {"NUMBER", "DECIMAL", "NUMERIC"}:
        return None
    return FIXED_TRACE_LENGTHS.get(type_name)
