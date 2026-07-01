from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decode import DecodeError, SUPPORTED_OBSERVED_TYPE_NAMES, decode_observed_row_values
from .metadata import CalibratedMetadata, ColumnMeta, StoragePageRef, TableMeta
from .page import ObservedPageHeader
from .row import iter_observed_rows_by_slots, scan_observed_row_chain
from .storage import DataFile


@dataclass(frozen=True)
class PagePlan:
    pages: tuple[StoragePageRef, ...]
    diagnostics: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ExtractionReport:
    table: str
    output: Path
    rows_written: int
    rows_skipped_deleted: int
    rows_skipped_decode_error: int
    decode_errors: tuple[str, ...]
    diagnostics: tuple[dict[str, Any], ...]
    scanned_pages: tuple[int, ...]
    scanned_page_refs: tuple[dict[str, int], ...]
    mode: str

    @property
    def ok(self) -> bool:
        return not any(item.get("level") == "error" for item in self.diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "output": str(self.output),
            "ok": self.ok,
            "rows_written": self.rows_written,
            "rows_skipped_deleted": self.rows_skipped_deleted,
            "rows_skipped_decode_error": self.rows_skipped_decode_error,
            "decode_errors": list(self.decode_errors),
            "diagnostics": list(self.diagnostics),
            "scanned_pages": list(self.scanned_pages),
            "scanned_page_refs": list(self.scanned_page_refs),
            "mode": self.mode,
        }


def extract_csv_with_calibrated_metadata(
    *,
    metadata: CalibratedMetadata,
    table_name: str,
    output: Path,
    page_plan_fallback_level: str | None = None,
    initial_diagnostics: tuple[dict[str, Any], ...] = (),
    delimiter: str = ",",
    include_sql_header: bool = False,
) -> ExtractionReport:
    """Create a CSV for a table using calibrated metadata.

    This is a transitional scaffold. It writes headers now and establishes the
    command/data flow; row scanning and decoding will be added as page and row
    structures are completed.
    """

    table = metadata.find_table(table_name)
    data_file_meta = metadata.find_data_file(
        table.storage.group_id,
        table.storage.file_no,
    )
    data_files = {
        item.file_no: DataFile(item.path, page_size=item.page_size)
        for item in metadata.data_files
        if item.group_id == table.storage.group_id
    }
    data_file = DataFile(data_file_meta.path, page_size=data_file_meta.page_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    rows_skipped_deleted = 0
    rows_skipped_decode_error = 0
    pages_skipped_non_data = 0
    pages_skipped_storage_mismatch = 0
    decode_errors: list[str] = []
    diagnostics: list[dict[str, Any]] = list(initial_diagnostics)
    accepted_page_refs: list[StoragePageRef] = []
    page_plan = _build_page_plan(
        table,
        data_files,
        fallback_level=page_plan_fallback_level,
    )
    diagnostics.extend(page_plan.diagnostics)
    unsupported_types = _unsupported_column_types(table)
    if unsupported_types:
        diagnostics.append(
            {
                "level": "error",
                "code": "unsupported-column-type",
                "message": "one or more table columns use types unsupported by the observed row decoder",
                "columns": [
                    {"name": column.name, "type_name": column.type_name}
                    for column in unsupported_types
                ],
            }
        )
    with output.open("w", newline="", encoding="utf-8") as file:
        if include_sql_header:
            file.write(_create_table_sql(table) + "\n")
            file.write("-- DATA\n")
        writer = csv.writer(file, delimiter=delimiter, lineterminator="\n")
        writer.writerow([column.name for column in table.columns])
        if unsupported_types:
            page_plan = PagePlan(pages=(), diagnostics=page_plan.diagnostics)
        for page_ref in page_plan.pages:
            page_no = page_ref.page_no
            page_file = data_files[page_ref.file_no]
            page = page_file.read_page(page_no)
            if table.storage.storage_id is not None:
                header = ObservedPageHeader.from_page(page)
                if header.page_kind_raw != 0x14:
                    pages_skipped_non_data += 1
                    continue
                if header.storage_id_candidate != table.storage.storage_id:
                    pages_skipped_storage_mismatch += 1
                    continue
            accepted_page_refs.append(page_ref)
            physical_rows = scan_observed_row_chain(page)
            rows_skipped_deleted += sum(1 for row in physical_rows if row.is_deleted)
            rows = iter_observed_rows_by_slots(page) or [
                row for row in physical_rows if not row.is_deleted
            ]
            for row in rows:
                try:
                    values = decode_observed_row_values(row, table.columns)
                except DecodeError as exc:
                    rows_skipped_decode_error += 1
                    diagnostic_code = exc.code
                    if not any(item.get("code") == diagnostic_code for item in diagnostics):
                        diagnostics.append(
                            {
                                "level": "error",
                                "code": diagnostic_code,
                                "message": _decode_error_message(diagnostic_code),
                            }
                        )
                    if len(decode_errors) < 10:
                        decode_errors.append(
                            f"page={page_no} offset={row.page_offset}: {exc}"
                        )
                    continue
                writer.writerow(values)
                rows_written += 1

    if pages_skipped_non_data:
        diagnostics.append(
            {
                "level": "info",
                "code": "page-scan-skipped-non-data-pages",
                "message": "one or more planned pages were not BTREE data pages and were skipped",
                "count": pages_skipped_non_data,
            }
        )
    if pages_skipped_storage_mismatch:
        diagnostics.append(
            {
                "level": "info",
                "code": "page-scan-skipped-storage-id-mismatch",
                "message": "one or more planned pages did not match the table storage id and were skipped",
                "count": pages_skipped_storage_mismatch,
                "storage_id": table.storage.storage_id,
            }
        )

    return ExtractionReport(
        table=table.qualified_name,
        output=output,
        rows_written=rows_written,
        rows_skipped_deleted=rows_skipped_deleted,
        rows_skipped_decode_error=rows_skipped_decode_error,
        decode_errors=tuple(decode_errors),
        diagnostics=tuple(diagnostics),
        scanned_pages=tuple(page_ref.page_no for page_ref in accepted_page_refs),
        scanned_page_refs=tuple(
            {"file_no": page_ref.file_no, "page_no": page_ref.page_no}
            for page_ref in accepted_page_refs
        ),
        mode=(
            "segment-manifest-page-ref-walk"
            if table.storage.page_numbers or table.storage.page_refs
            else "calibrated-metadata-page-range-scan"
        ),
    )


def _create_table_sql(table: TableMeta) -> str:
    owner_prefix = f"{table.owner}." if table.owner else ""
    column_lines = []
    for column in table.columns:
        column_lines.append(f"  {column.name} {_ddl_type(column)}")
    columns_sql = ",\n".join(column_lines)
    return f"CREATE TABLE {owner_prefix}{table.name} (\n{columns_sql}\n);"


def _ddl_type(column: ColumnMeta) -> str:
    type_name = column.type_name.upper()
    if column.length is not None and type_name in {"CHAR", "VARCHAR", "VARCHAR2", "BINARY", "VARBINARY"}:
        return f"{type_name}({column.length})"
    return type_name


def _unsupported_column_types(table: TableMeta) -> tuple[ColumnMeta, ...]:
    return tuple(
        column
        for column in table.columns
        if column.type_name.upper() not in SUPPORTED_OBSERVED_TYPE_NAMES
    )


def _decode_error_message(code: str) -> str:
    if code == "unsupported-row-metadata":
        return (
            "one or more live rows contain row metadata not supported by the "
            "observed decoder"
        )
    return "one or more live rows could not be decoded"


def describe_table_plan(table: TableMeta) -> list[str]:
    return [
        f"table={table.qualified_name}",
        (
            "storage="
            f"group:{table.storage.group_id},"
            f"file:{table.storage.file_no},"
            f"root_page:{table.storage.root_page}"
            f",scan_pages:{table.storage.scan_pages}"
            f",page_numbers:{','.join(str(value) for value in table.storage.page_numbers) or '-'}"
        ),
        "columns=" + ",".join(f"{col.name}:{col.type_name}" for col in table.columns),
    ]


def _build_page_plan(
    table: TableMeta,
    data_files: dict[int, DataFile],
    *,
    fallback_level: str | None = None,
) -> PagePlan:
    if table.storage.page_refs:
        return _walk_leaf_chain(
            data_files=data_files,
            start_pages=table.storage.page_refs,
        )
    if table.storage.page_numbers:
        return _walk_same_file_leaf_chain(
            data_file=data_files[table.storage.file_no],
            file_no=table.storage.file_no,
            start_pages=table.storage.page_numbers,
        )
    if table.storage.storage_id is not None:
        root_plan = _build_root_page_plan(table=table, data_files=data_files)
        if root_plan.pages or any(item.get("level") == "error" for item in root_plan.diagnostics):
            return root_plan
        storage_scan_plan = _build_storage_id_page_plan(table=table, data_files=data_files)
        return PagePlan(
            pages=storage_scan_plan.pages,
            diagnostics=root_plan.diagnostics + storage_scan_plan.diagnostics,
        )
    diagnostics: tuple[dict[str, Any], ...] = ()
    if fallback_level is not None:
        diagnostics = (
            {
                "level": fallback_level,
                "code": "page-plan-fallback-scan-range",
                "message": "segment manifest has no page-reference plan and no storage id; falling back to scan_pages from the root page",
                "root_page": table.storage.root_page,
                "scan_pages": table.storage.scan_pages,
            },
        )
    return PagePlan(
        pages=tuple(
            StoragePageRef(file_no=table.storage.file_no, page_no=page_no)
            for page_no in _iter_scan_pages(table)
        ),
        diagnostics=diagnostics,
    )




def _build_root_page_plan(
    *,
    table: TableMeta,
    data_files: dict[int, DataFile],
) -> PagePlan:
    data_file = data_files.get(table.storage.file_no)
    if data_file is None:
        return PagePlan(
            pages=(),
            diagnostics=(
                {
                    "level": "error",
                    "code": "page-plan-file-missing",
                    "message": "storage root file is not present in metadata",
                    "file_no": table.storage.file_no,
                    "root_page": table.storage.root_page,
                },
            ),
        )
    pages_total = data_file.path.stat().st_size // data_file.page_size
    root_page_no = table.storage.root_page
    if root_page_no < 0 or root_page_no >= pages_total:
        return PagePlan(
            pages=(),
            diagnostics=(
                {
                    "level": "error",
                    "code": "page-plan-root-out-of-range",
                    "message": "storage root page is outside the data file",
                    "file_no": table.storage.file_no,
                    "root_page": root_page_no,
                    "pages_total": pages_total,
                },
            ),
        )
    root_page = data_file.read_page(root_page_no)
    root_header = ObservedPageHeader.from_page(root_page)
    if root_header.storage_id_candidate != table.storage.storage_id:
        return PagePlan(
            pages=(),
            diagnostics=(
                {
                    "level": "warning",
                    "code": "page-plan-root-storage-id-mismatch",
                    "message": "root page storage id did not match table storage id; storage-id scan fallback is required",
                    "root_page": root_page_no,
                    "expected_storage_id": table.storage.storage_id,
                    "observed_storage_id": root_header.storage_id_candidate,
                },
            ),
        )
    if root_header.page_kind_raw == 0x14:
        plan = _walk_same_file_leaf_chain(
            data_file=data_file,
            file_no=table.storage.file_no,
            start_pages=(root_page_no,),
        )
        return PagePlan(
            pages=plan.pages,
            diagnostics=(
                {
                    "level": "info",
                    "code": "page-plan-root-leaf-chain",
                    "message": "planned BTREE data pages by walking the root leaf page chain",
                    "root_page": root_page_no,
                    "storage_id": table.storage.storage_id,
                    "pages_planned": len(plan.pages),
                },
            ) + plan.diagnostics,
        )
    if root_header.page_kind_raw == 0x15:
        leftmost_child = _btree_root_leftmost_child(root_page)
        entry_children = _btree_root_entry_child_pages(root_page, root_header.observed_row_count)
        if leftmost_child is None:
            return PagePlan(
                pages=(),
                diagnostics=(
                    {
                        "level": "warning",
                        "code": "page-plan-btree-root-no-leftmost-child",
                        "message": "BTREE root page did not contain a usable leftmost child pointer; storage-id scan fallback is required",
                        "root_page": root_page_no,
                        "storage_id": table.storage.storage_id,
                        "entry_count": root_header.observed_row_count,
                    },
                ),
            )
        plan = _walk_same_file_leaf_chain(
            data_file=data_file,
            file_no=table.storage.file_no,
            start_pages=(leftmost_child,),
        )
        planned_pages = tuple(page_ref.page_no for page_ref in plan.pages)
        diagnostics: list[dict[str, Any]] = [
            {
                "level": "info",
                "code": "page-plan-btree-root-children",
                "message": "planned BTREE data pages from a BTREE root/internal page and leaf next-chain",
                "root_page": root_page_no,
                "storage_id": table.storage.storage_id,
                "leftmost_child_page": leftmost_child,
                "root_entry_count": root_header.observed_row_count,
                "root_entry_child_pages": entry_children,
                "pages_planned": len(plan.pages),
            }
        ]
        entry_set = set(entry_children)
        planned_set = set(planned_pages)
        if entry_set and entry_set != planned_set - {leftmost_child}:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "page-plan-btree-root-entry-mismatch",
                    "message": "BTREE root child entries did not exactly match the walked leaf chain",
                    "root_page": root_page_no,
                    "entry_child_pages": entry_children,
                    "walked_pages": list(planned_pages),
                }
            )
        diagnostics.extend(plan.diagnostics)
        return PagePlan(pages=plan.pages, diagnostics=tuple(diagnostics))
    return PagePlan(
        pages=(),
        diagnostics=(
            {
                "level": "info",
                "code": "page-plan-root-kind-unhandled",
                "message": "root page kind is not yet parsed; storage-id scan fallback is required",
                "root_page": root_page_no,
                "storage_id": table.storage.storage_id,
                "page_kind_raw": root_header.page_kind_raw,
            },
        ),
    )


def _btree_root_leftmost_child(page: bytes) -> int | None:
    if len(page) < 0x56:
        return None
    value = int.from_bytes(page[0x52:0x56], "little")
    return value if value > 0 else None


def _btree_root_entry_child_pages(page: bytes, entry_count: int) -> list[int]:
    if entry_count <= 0:
        return []
    slot_start = len(page) - 10 - (entry_count * 2)
    if slot_start < 0:
        return []
    child_pages: list[int] = []
    for slot_offset in range(slot_start, slot_start + entry_count * 2, 2):
        entry_offset = int.from_bytes(page[slot_offset : slot_offset + 2], "little")
        if entry_offset <= 0 or entry_offset + 7 > len(page):
            continue
        child_page = int.from_bytes(page[entry_offset + 3 : entry_offset + 7], "little")
        if child_page > 0:
            child_pages.append(child_page)
    child_pages.sort()
    return child_pages


def _build_storage_id_page_plan(
    *,
    table: TableMeta,
    data_files: dict[int, DataFile],
) -> PagePlan:
    data_file = data_files.get(table.storage.file_no)
    if data_file is None:
        return PagePlan(
            pages=(),
            diagnostics=(
                {
                    "level": "error",
                    "code": "page-plan-file-missing",
                    "message": "storage root file is not present in metadata",
                    "file_no": table.storage.file_no,
                    "root_page": table.storage.root_page,
                },
            ),
        )
    pages_total = data_file.path.stat().st_size // data_file.page_size
    planned: list[StoragePageRef] = []
    skipped_non_data = 0
    skipped_storage_mismatch = 0
    for page_no in _iter_scan_pages(table):
        if page_no < 0 or page_no >= pages_total:
            break
        page = data_file.read_page(page_no)
        if not any(page):
            continue
        header = ObservedPageHeader.from_page(page)
        if header.file_no_hint != table.storage.file_no or header.page_no != page_no:
            continue
        if header.page_kind_raw != 0x14:
            skipped_non_data += 1
            continue
        if header.storage_id_candidate != table.storage.storage_id:
            skipped_storage_mismatch += 1
            continue
        planned.append(StoragePageRef(file_no=table.storage.file_no, page_no=page_no))
    diagnostics: list[dict[str, Any]] = []
    if not planned:
        diagnostics.append(
            {
                "level": "error",
                "code": "page-plan-storage-id-no-data-pages",
                "message": "no BTREE data pages matched the table storage id in the segment scan window",
                "root_page": table.storage.root_page,
                "scan_pages": table.storage.scan_pages,
                "storage_id": table.storage.storage_id,
            }
        )
    else:
        diagnostics.append(
            {
                "level": "info",
                "code": "page-plan-storage-id-scan",
                "message": "planned BTREE data pages by matching page-header storage id in the segment window",
                "root_page": table.storage.root_page,
                "scan_pages": table.storage.scan_pages,
                "storage_id": table.storage.storage_id,
                "pages_planned": len(planned),
                "skipped_non_data": skipped_non_data,
                "skipped_storage_mismatch": skipped_storage_mismatch,
            }
        )
    return PagePlan(pages=tuple(planned), diagnostics=tuple(diagnostics))


def _iter_scan_pages(table: TableMeta) -> range:
    return range(
        table.storage.root_page,
        table.storage.root_page + table.storage.scan_pages,
    )


def _walk_same_file_leaf_chain(
    *,
    data_file: DataFile,
    file_no: int,
    start_pages: tuple[int, ...],
) -> PagePlan:
    return _walk_leaf_chain(
        data_files={file_no: data_file},
        start_pages=tuple(
            StoragePageRef(file_no=file_no, page_no=page_no)
            for page_no in start_pages
        ),
    )


def _walk_leaf_chain(
    *,
    data_files: dict[int, DataFile],
    start_pages: tuple[StoragePageRef, ...],
) -> PagePlan:
    pages_total_by_file = {
        file_no: data_file.path.stat().st_size // data_file.page_size
        for file_no, data_file in data_files.items()
    }
    result: list[StoragePageRef] = []
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    start_keys = {(item.file_no, item.page_no) for item in start_pages}
    for start_page in start_pages:
        page_ref: StoragePageRef | None = start_page
        while page_ref is not None:
            file_no = page_ref.file_no
            page_no = page_ref.page_no
            data_file = data_files.get(file_no)
            if data_file is None:
                diagnostics.append(
                    {
                        "level": "error",
                        "code": "page-plan-file-missing",
                        "message": "planned page references a file not present in metadata",
                        "file_no": file_no,
                        "page_no": page_no,
                    }
                )
                break
            pages_total = pages_total_by_file[file_no]
            if page_no < 0 or page_no >= pages_total:
                diagnostics.append(
                    {
                        "level": "error",
                        "code": "page-plan-out-of-range",
                        "message": "planned page is outside the data file",
                        "file_no": file_no,
                        "page_no": page_no,
                        "pages_total": pages_total,
                    }
                )
                break
            key = (file_no, page_no)
            if key in seen:
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": "page-plan-cycle",
                        "message": "page traversal stopped after reaching an already scanned page",
                        "file_no": file_no,
                        "page_no": page_no,
                    }
                )
                break
            page = data_file.read_page(page_no)
            header = ObservedPageHeader.from_page(page)
            if header.file_no_hint != file_no or header.page_no != page_no:
                diagnostics.append(
                    {
                        "level": "error",
                        "code": "page-plan-identity-mismatch",
                        "message": "planned page header identity does not match expected file/page",
                        "expected_file_no": file_no,
                        "expected_page_no": page_no,
                        "observed_file_no": header.file_no_hint,
                        "observed_page_no": header.page_no,
                    }
                )
                break
            if header.page_kind_label != "tentative-btree-data":
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": (
                            "page-plan-start-non-data"
                            if key in start_keys
                            else "page-plan-non-leaf-stop"
                        ),
                        "message": (
                            "planned start page is not a BTREE/data page"
                            if key in start_keys
                            else "page traversal stopped at a non-BTREE/data page"
                        ),
                        "file_no": file_no,
                        "page_no": page_no,
                        "page_type_raw": header.page_type_raw,
                        "page_kind_raw": header.page_kind_raw,
                        "page_kind_label": header.page_kind_label,
                    }
                )
                break
            seen.add(key)
            result.append(page_ref)
            if header.next_page.is_null:
                break
            page_ref = StoragePageRef(
                file_no=header.next_page.file_no,
                page_no=header.next_page.page_no,
            )
    return PagePlan(pages=tuple(result), diagnostics=tuple(diagnostics))
