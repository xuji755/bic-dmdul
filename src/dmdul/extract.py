from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decode import DecodeError, decode_observed_row_values
from .metadata import CalibratedMetadata, TableMeta
from .page import ObservedPageHeader
from .row import scan_observed_row_chain
from .storage import DataFile


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
            "mode": self.mode,
        }


def extract_csv_with_calibrated_metadata(
    *,
    metadata: CalibratedMetadata,
    table_name: str,
    output: Path,
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
    data_file = DataFile(data_file_meta.path, page_size=data_file_meta.page_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    rows_skipped_deleted = 0
    rows_skipped_decode_error = 0
    decode_errors: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    page_numbers = _iter_table_pages(table, data_file)
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([column.name for column in table.columns])
        for page_no in page_numbers:
            page = data_file.read_page(page_no)
            rows = scan_observed_row_chain(page)
            for row in rows:
                if row.is_deleted:
                    rows_skipped_deleted += 1
                    continue
                try:
                    values = decode_observed_row_values(row, table.columns)
                except DecodeError as exc:
                    rows_skipped_decode_error += 1
                    if not any(item["code"] == "row-decode-error" for item in diagnostics):
                        diagnostics.append(
                            {
                                "level": "error",
                                "code": "row-decode-error",
                                "message": "one or more live rows could not be decoded",
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
        scanned_pages=tuple(page_numbers),
        mode=(
            "segment-manifest-page-ref-walk"
            if table.storage.page_numbers
            else "calibrated-metadata-page-range-scan"
        ),
    )


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


def _iter_table_pages(table: TableMeta, data_file: DataFile) -> tuple[int, ...]:
    if table.storage.page_numbers:
        return _walk_same_file_leaf_chain(
            data_file=data_file,
            file_no=table.storage.file_no,
            start_pages=table.storage.page_numbers,
        )
    return tuple(_iter_scan_pages(table))


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
) -> tuple[int, ...]:
    pages_total = data_file.path.stat().st_size // data_file.page_size
    result: list[int] = []
    seen: set[int] = set()
    for start_page in start_pages:
        page_no: int | None = start_page
        while page_no is not None:
            if page_no < 0 or page_no >= pages_total or page_no in seen:
                break
            page = data_file.read_page(page_no)
            header = ObservedPageHeader.from_page(page)
            if header.file_no_hint != file_no or header.page_no != page_no:
                break
            seen.add(page_no)
            result.append(page_no)
            if (
                header.page_kind_label != "tentative-btree-data"
                or header.next_page.is_null
                or header.next_page.file_no != file_no
            ):
                break
            page_no = header.next_page.page_no
    return tuple(result)
