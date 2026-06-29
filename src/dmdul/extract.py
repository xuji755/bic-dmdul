from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .decode import DecodeError, decode_observed_row_values
from .metadata import CalibratedMetadata, TableMeta
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
    mode: str


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
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([column.name for column in table.columns])
        for page_no in _iter_scan_pages(table):
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
        mode="calibrated-metadata-page-range-scan",
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
        ),
        "columns=" + ",".join(f"{col.name}:{col.type_name}" for col in table.columns),
    ]


def _iter_scan_pages(table: TableMeta) -> range:
    return range(
        table.storage.root_page,
        table.storage.root_page + table.storage.scan_pages,
    )
