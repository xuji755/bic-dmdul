from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decode import DecodeError, SUPPORTED_OBSERVED_TYPE_NAMES, decode_observed_row_values
from .metadata import CalibratedMetadata, ColumnMeta, StoragePageRef, TableMeta
from .page import ObservedPageHeader
from .row import scan_observed_row_chain
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
    decode_errors: list[str] = []
    diagnostics: list[dict[str, Any]] = list(initial_diagnostics)
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
        writer = csv.writer(file)
        writer.writerow([column.name for column in table.columns])
        if unsupported_types:
            page_plan = PagePlan(pages=(), diagnostics=page_plan.diagnostics)
        for page_ref in page_plan.pages:
            page_no = page_ref.page_no
            page_file = data_files[page_ref.file_no]
            page = page_file.read_page(page_no)
            rows = scan_observed_row_chain(page)
            for row in rows:
                if row.is_deleted:
                    rows_skipped_deleted += 1
                    continue
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

    return ExtractionReport(
        table=table.qualified_name,
        output=output,
        rows_written=rows_written,
        rows_skipped_deleted=rows_skipped_deleted,
        rows_skipped_decode_error=rows_skipped_decode_error,
        decode_errors=tuple(decode_errors),
        diagnostics=tuple(diagnostics),
        scanned_pages=tuple(page_ref.page_no for page_ref in page_plan.pages),
        scanned_page_refs=tuple(
            {"file_no": page_ref.file_no, "page_no": page_ref.page_no}
            for page_ref in page_plan.pages
        ),
        mode=(
            "segment-manifest-page-ref-walk"
            if table.storage.page_numbers or table.storage.page_refs
            else "calibrated-metadata-page-range-scan"
        ),
    )


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
    diagnostics: tuple[dict[str, Any], ...] = ()
    if fallback_level is not None:
        diagnostics = (
            {
                "level": fallback_level,
                "code": "page-plan-fallback-scan-range",
                "message": "segment manifest has no page-reference plan; falling back to scan_pages from the root page",
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
            seen.add(key)
            result.append(page_ref)
            if header.page_kind_label != "tentative-btree-data":
                if key not in start_keys:
                    diagnostics.append(
                        {
                            "level": "warning",
                            "code": "page-plan-non-leaf-stop",
                            "message": "page traversal stopped at a non-BTREE/data page",
                            "file_no": file_no,
                            "page_no": page_no,
                            "page_kind_raw": header.page_kind_raw,
                            "page_kind_label": header.page_kind_label,
                        }
                    )
                break
            if header.next_page.is_null:
                break
            page_ref = StoragePageRef(
                file_no=header.next_page.file_no,
                page_no=header.next_page.page_no,
            )
    return PagePlan(pages=tuple(result), diagnostics=tuple(diagnostics))
