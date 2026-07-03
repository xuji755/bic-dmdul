from __future__ import annotations

import csv
import hashlib
import json
import mmap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .decode import (
    DecodeError,
    LobValue,
    SUPPORTED_OBSERVED_TYPE_NAMES,
    decode_character_bytes_with_encoding,
    decode_observed_row_values,
)
from .lob import LobReadError, read_out_of_line_lob
from .metadata import CalibratedMetadata, ColumnMeta, StoragePageRef, TableMeta
from .page import ObservedPageHeader
from .row import iter_observed_rows_by_slots, scan_observed_row_chain
from .row_archive import RowArchiveWriter
from .storage import DataFile


@dataclass(frozen=True)
class PagePlan:
    pages: tuple[StoragePageRef, ...]
    diagnostics: tuple[dict[str, Any], ...]
    mode: str


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

    @property
    def strict_failures(self) -> tuple[dict[str, Any], ...]:
        failures: list[dict[str, Any]] = []
        for diagnostic in self.diagnostics:
            level = diagnostic.get("level")
            code = str(diagnostic.get("code") or "")
            if level == "error" or code in STRICT_UNCERTAIN_DIAGNOSTIC_CODES:
                failures.append(diagnostic)
        if self.rows_skipped_decode_error and not any(
            item.get("code") in {"row-decode-error", "unsupported-row-metadata"}
            for item in failures
        ):
            failures.append(
                {
                    "level": "error",
                    "code": "row-decode-errors-present",
                    "message": "one or more live rows could not be decoded",
                    "count": self.rows_skipped_decode_error,
                }
            )
        return tuple(failures)

    @property
    def strict_ok(self) -> bool:
        return not self.strict_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "output": str(self.output),
            "ok": self.ok,
            "strict_ok": self.strict_ok,
            "strict_failures": list(self.strict_failures),
            "rows_written": self.rows_written,
            "rows_skipped_deleted": self.rows_skipped_deleted,
            "rows_skipped_decode_error": self.rows_skipped_decode_error,
            "decode_errors": list(self.decode_errors),
            "diagnostics": list(self.diagnostics),
            "scanned_pages": list(self.scanned_pages),
            "scanned_page_refs": list(self.scanned_page_refs),
            "mode": self.mode,
        }


@dataclass(frozen=True)
class LobExportOptions:
    mode: str = "inline"
    directory: Path | None = None
    manifest_name: str = "manifest.jsonl"
    hash_name: str = "sha256"


@dataclass(frozen=True)
class LobWriteResult:
    placeholder: str
    status: str


@dataclass(frozen=True)
class PageScanResult:
    page_ref: StoragePageRef
    accepted: bool
    rows: tuple[tuple[Any, list[object]], ...] = ()
    rows_skipped_deleted: int = 0
    rows_skipped_decode_error: int = 0
    pages_skipped_non_data: int = 0
    pages_skipped_storage_mismatch: int = 0
    decode_errors: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()


STORAGE_SCAN_PROGRESS_PAGES = 65536


STRICT_UNCERTAIN_DIAGNOSTIC_CODES = frozenset(
    {
        "page-plan-fallback-scan-range",
        "segment-manifest-data-file-without-control-entry",
        "page-plan-btree-root-entry-mismatch",
        "page-scan-skipped-non-data-pages",
        "page-scan-skipped-storage-id-mismatch",
    }
)


def extract_csv_with_calibrated_metadata(
    *,
    metadata: CalibratedMetadata,
    table_name: str,
    output: Path,
    page_plan_fallback_level: str | None = None,
    initial_diagnostics: tuple[dict[str, Any], ...] = (),
    delimiter: str = ",",
    include_sql_header: bool = False,
    lob_mode: str = "inline",
    lob_dir: Path | None = None,
    lob_hash: str = "sha256",
    output_format: str = "dul",
    progress: Callable[[dict[str, Any]], None] | None = None,
    progress_interval_pages: int = 64,
    page_workers: int = 1,
    page_refs_override: tuple[StoragePageRef, ...] | None = None,
    partition_names: tuple[str, ...] = (),
    empty_page_plan_level: str | None = None,
    orphan_scan_storage_id: int | None = None,
    orphan_scan_storage_ids: tuple[int, ...] = (),
) -> ExtractionReport:
    """Create a CSV for a table using calibrated metadata.

    This is a transitional scaffold. It writes headers now and establishes the
    command/data flow; row scanning and decoding will be added as page and row
    structures are completed.
    """

    if output_format not in {"dul", "row"}:
        raise ValueError(f"unsupported output format: {output_format}")
    table = metadata.find_table(table_name)
    orphan_storage_ids = _normalize_orphan_storage_ids(
        orphan_scan_storage_id=orphan_scan_storage_id,
        orphan_scan_storage_ids=orphan_scan_storage_ids,
    )
    if orphan_storage_ids:
        storage_id_for_scan_validation = orphan_storage_ids[0] if len(orphan_storage_ids) == 1 else None
        table = replace(
            table,
            storage=replace(table.storage, storage_id=storage_id_for_scan_validation),
        )
    data_file_meta = metadata.find_data_file(
        table.storage.group_id,
        table.storage.file_no,
    )
    data_files = {
        item.file_no: DataFile(item.path, page_size=item.page_size)
        for item in metadata.data_files
        if item.group_id == table.storage.group_id
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    lob_options = LobExportOptions(mode=lob_mode, directory=lob_dir, hash_name=lob_hash)
    lob_context: LobExportContext | None = None
    rows_written = 0
    rows_skipped_deleted = 0
    rows_skipped_decode_error = 0
    pages_skipped_non_data = 0
    pages_skipped_storage_mismatch = 0
    decode_errors: list[str] = []
    diagnostics: list[dict[str, Any]] = list(initial_diagnostics)
    accepted_page_refs: list[StoragePageRef] = []
    if orphan_storage_ids and page_refs_override is None:
        if partition_names:
            raise ValueError("partition_names cannot be used with orphan storage scan")
        page_plan = _build_orphan_storage_id_page_plan(
            table=table,
            data_files=data_files,
            storage_ids=orphan_storage_ids,
            progress=progress,
        )
    elif page_refs_override is None:
        page_plan = _build_page_plan(
            table,
            data_files,
            fallback_level=page_plan_fallback_level,
            progress=progress,
        )
        if partition_names:
            selected_refs = _select_partition_page_refs(
                table=table,
                partition_names=partition_names,
            )
            page_plan = PagePlan(
                pages=selected_refs,
                diagnostics=page_plan.diagnostics
                + (
                    {
                        "level": "info",
                        "code": "page-plan-partition-filter",
                        "message": "planned pages were filtered by requested partition names",
                        "partition_names": list(partition_names),
                        "pages_planned": len(selected_refs),
                    },
                ),
                mode=f"{page_plan.mode}-partition-filter",
            )
    else:
        page_plan = PagePlan(
            pages=page_refs_override,
            diagnostics=(),
            mode="explicit-page-ref-list",
        )
    diagnostics.extend(page_plan.diagnostics)
    if not page_plan.pages and empty_page_plan_level is not None:
        diagnostics.append(
            {
                "level": empty_page_plan_level,
                "code": "page-plan-empty",
                "message": "page planning produced no data pages; no rows can be exported",
                "mode": page_plan.mode,
            }
        )
    _emit_extract_progress(
        progress,
        {
            "event": "plan",
            "table": table.qualified_name,
            "pages_total": len(page_plan.pages),
            "output": str(output),
        },
    )
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
    open_mode = "wb" if output_format == "row" else "w"
    open_kwargs: dict[str, Any] = {}
    if output_format != "row":
        open_kwargs = {"newline": "", "encoding": "utf-8"}
    with output.open(open_mode, **open_kwargs) as file:
        lob_context = _open_lob_export_context(
            output=output,
            options=lob_options,
            data_files=data_files,
        )
        row_archive: RowArchiveWriter | None = None
        try:
            writer = None
            if output_format == "row":
                row_archive = RowArchiveWriter(file, table=table)
            elif include_sql_header:
                file.write(_create_table_sql(table) + "\n")
                file.write("-- DATA\n")
            if output_format != "row":
                writer = csv.writer(file, delimiter=delimiter, lineterminator="\n")
                writer.writerow([column.name for column in table.columns])
            if unsupported_types:
                page_plan = PagePlan(
                    pages=(),
                    diagnostics=page_plan.diagnostics,
                    mode=page_plan.mode,
                )
            pages_total = len(page_plan.pages)
            page_results = _iter_page_scan_results(
                page_refs=page_plan.pages,
                data_files=data_files,
                table=table,
                external_lobs=lob_options.mode != "inline" or output_format == "row",
                page_workers=page_workers,
            )
            for pages_done, page_result in enumerate(page_results, start=1):
                page_ref = page_result.page_ref
                page_no = page_ref.page_no
                rows_skipped_deleted += page_result.rows_skipped_deleted
                rows_skipped_decode_error += page_result.rows_skipped_decode_error
                pages_skipped_non_data += page_result.pages_skipped_non_data
                pages_skipped_storage_mismatch += page_result.pages_skipped_storage_mismatch
                for diagnostic in page_result.diagnostics:
                    diagnostic_code = diagnostic.get("code")
                    if not any(item.get("code") == diagnostic_code for item in diagnostics):
                        diagnostics.append(diagnostic)
                for error in page_result.decode_errors:
                    if len(decode_errors) < 10:
                        decode_errors.append(error)
                if not page_result.accepted:
                    continue
                accepted_page_refs.append(page_ref)
                for row, values in page_result.rows:
                    row_sequence = rows_written + 1
                    if row_archive is not None:
                        _write_row_archive_lobs(
                            archive=row_archive,
                            values=values,
                            table=table,
                            data_files=data_files,
                            row_sequence=row_sequence,
                            diagnostics=diagnostics,
                        )
                        row_archive.write_row(
                            row_sequence=row_sequence,
                            file_no=page_ref.file_no,
                            page_no=page_no,
                            row_offset=row.page_offset,
                            row_data=row.data,
                        )
                    else:
                        values = _export_lob_values(
                            values=values,
                            table=table,
                            row_sequence=row_sequence,
                            context=lob_context,
                            diagnostics=diagnostics,
                        )
                        assert writer is not None
                        writer.writerow(values)
                    rows_written += 1
                if _should_report_page_progress(
                    pages_done=pages_done,
                    pages_total=pages_total,
                    progress_interval_pages=progress_interval_pages,
                ):
                    file.flush()
                    _emit_extract_progress(
                        progress,
                        {
                            "event": "block",
                            "table": table.qualified_name,
                            "pages_done": pages_done,
                            "pages_total": pages_total,
                            "file_no": page_ref.file_no,
                            "page_no": page_ref.page_no,
                            "rows_written": rows_written,
                            "output": str(output),
                        },
                    )
        finally:
            if row_archive is not None:
                row_archive.write_end(rows=rows_written)
            if lob_context is not None:
                lob_context.close()

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

    report = ExtractionReport(
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
        mode=page_plan.mode,
    )
    _emit_extract_progress(
        progress,
        {
            "event": "complete",
            "table": report.table,
            "ok": report.ok,
            "rows_written": report.rows_written,
            "rows_skipped_deleted": report.rows_skipped_deleted,
            "rows_skipped_decode_error": report.rows_skipped_decode_error,
            "pages_done": len(report.scanned_page_refs),
            "output": str(report.output),
        },
    )
    return report


def extract_split_parts_with_calibrated_metadata(
    *,
    metadata: CalibratedMetadata,
    table_name: str,
    output: Path,
    part_workers: int,
    page_plan_fallback_level: str | None = None,
    initial_diagnostics: tuple[dict[str, Any], ...] = (),
    delimiter: str = ",",
    lob_mode: str = "inline",
    lob_hash: str = "sha256",
    output_format: str = "dul",
    progress: Callable[[dict[str, Any]], None] | None = None,
    partition_names: tuple[str, ...] = (),
    empty_page_plan_level: str | None = None,
) -> ExtractionReport:
    if output_format not in {"dul", "row"}:
        raise ValueError(f"unsupported output format: {output_format}")
    table = metadata.find_table(table_name)
    data_files = {
        item.file_no: DataFile(item.path, page_size=item.page_size)
        for item in metadata.data_files
        if item.group_id == table.storage.group_id
    }
    page_plan = _build_page_plan(
        table,
        data_files,
        fallback_level=page_plan_fallback_level,
        progress=progress,
    )
    if partition_names:
        selected_refs = _select_partition_page_refs(
            table=table,
            partition_names=partition_names,
        )
        page_plan = PagePlan(
            pages=selected_refs,
            diagnostics=page_plan.diagnostics
            + (
                {
                    "level": "info",
                    "code": "page-plan-partition-filter",
                    "message": "planned pages were filtered by requested partition names",
                    "partition_names": list(partition_names),
                    "pages_planned": len(selected_refs),
                },
            ),
            mode=f"{page_plan.mode}-partition-filter",
        )
    if not page_plan.pages and empty_page_plan_level is not None:
        page_plan = PagePlan(
            pages=page_plan.pages,
            diagnostics=page_plan.diagnostics
            + (
                {
                    "level": empty_page_plan_level,
                    "code": "page-plan-empty",
                    "message": "page planning produced no data pages; no rows can be exported",
                    "mode": page_plan.mode,
                },
            ),
            mode=page_plan.mode,
        )
    workers = max(1, part_workers)
    chunks = _split_page_refs(page_plan.pages, workers)
    output.parent.mkdir(parents=True, exist_ok=True)
    part_dir = output.with_suffix(output.suffix + ".parts")
    part_dir.mkdir(parents=True, exist_ok=True)
    extension = "row" if output_format == "row" else "dul"
    reports: list[ExtractionReport] = []
    diagnostics: list[dict[str, Any]] = list(initial_diagnostics)
    diagnostics.extend(page_plan.diagnostics)

    def export_part(item: tuple[int, tuple[StoragePageRef, ...]]) -> ExtractionReport:
        part_index, page_refs = item
        part_output = part_dir / f"part-{part_index:06d}.{extension}"
        return extract_csv_with_calibrated_metadata(
            metadata=metadata,
            table_name=table.qualified_name,
            output=part_output,
            page_plan_fallback_level=page_plan_fallback_level,
            delimiter=delimiter,
            include_sql_header=True,
            lob_mode=lob_mode,
            lob_hash=lob_hash,
            output_format=output_format,
            progress=progress,
            page_refs_override=page_refs,
        )

    part_items = tuple((index, chunk) for index, chunk in enumerate(chunks, start=1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for part_item, future in zip(part_items, executor.map(export_part, part_items)):
            reports.append(future)

    reports.sort(key=lambda report: str(report.output))
    for report in reports:
        diagnostics.extend(report.diagnostics)
    _write_parts_manifest(
        output=output,
        table=table,
        output_format=output_format,
        delimiter=delimiter,
        part_dir=part_dir,
        reports=tuple(reports),
    )
    rows_written = sum(report.rows_written for report in reports)
    rows_skipped_deleted = sum(report.rows_skipped_deleted for report in reports)
    rows_skipped_decode_error = sum(report.rows_skipped_decode_error for report in reports)
    decode_errors: list[str] = []
    scanned_page_refs: list[dict[str, int]] = []
    for report in reports:
        for error in report.decode_errors:
            if len(decode_errors) < 10:
                decode_errors.append(error)
        scanned_page_refs.extend(report.scanned_page_refs)
    return ExtractionReport(
        table=table.qualified_name,
        output=output,
        rows_written=rows_written,
        rows_skipped_deleted=rows_skipped_deleted,
        rows_skipped_decode_error=rows_skipped_decode_error,
        decode_errors=tuple(decode_errors),
        diagnostics=tuple(diagnostics),
        scanned_pages=tuple(item["page_no"] for item in scanned_page_refs),
        scanned_page_refs=tuple(scanned_page_refs),
        mode=f"{page_plan.mode}-split-parts",
    )


def _split_page_refs(
    page_refs: tuple[StoragePageRef, ...],
    workers: int,
) -> tuple[tuple[StoragePageRef, ...], ...]:
    if not page_refs:
        return ()
    chunk_count = min(max(1, workers), len(page_refs))
    chunk_size = (len(page_refs) + chunk_count - 1) // chunk_count
    return tuple(
        page_refs[index : index + chunk_size]
        for index in range(0, len(page_refs), chunk_size)
    )


def _select_partition_page_refs(
    *,
    table: TableMeta,
    partition_names: tuple[str, ...],
) -> tuple[StoragePageRef, ...]:
    if not table.storage.partition_page_refs:
        raise ValueError(
            f"table {table.qualified_name} has no partition page-ref metadata"
        )
    refs_by_name = {
        item.name.casefold(): item.page_ref
        for item in table.storage.partition_page_refs
    }
    selected: list[StoragePageRef] = []
    missing: list[str] = []
    for name in partition_names:
        page_ref = refs_by_name.get(name.casefold())
        if page_ref is None:
            missing.append(name)
            continue
        selected.append(page_ref)
    if missing:
        available = ",".join(item.name for item in table.storage.partition_page_refs)
        raise ValueError(
            "partition name not found for "
            f"{table.qualified_name}: {','.join(missing)}; available={available}"
        )
    return tuple(selected)


def _write_parts_manifest(
    *,
    output: Path,
    table: TableMeta,
    output_format: str,
    delimiter: str,
    part_dir: Path,
    reports: tuple[ExtractionReport, ...],
) -> None:
    try:
        part_dir_value = str(part_dir.relative_to(output.parent))
    except ValueError:
        part_dir_value = str(part_dir)
    lines = [
        "DMDUL-PARTS 1",
        f"FORMAT {output_format}",
        f"TABLE {table.qualified_name}",
        f"DELIMITER {delimiter}",
        f"PART_DIR {part_dir_value}",
        f"PART_COUNT {len(reports)}",
        "CREATE_SQL_BEGIN",
        _create_table_sql(table).rstrip(),
        "CREATE_SQL_END",
    ]
    for index, report in enumerate(reports, start=1):
        try:
            part_value = str(report.output.relative_to(part_dir))
        except ValueError:
            part_value = str(report.output)
        lines.append(
            f"PART {index} {part_value} ROWS {report.rows_written} OK {str(report.ok).lower()}"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _iter_page_scan_results(
    *,
    page_refs: tuple[StoragePageRef, ...],
    data_files: dict[int, DataFile],
    table: TableMeta,
    external_lobs: bool,
    page_workers: int,
) -> Any:
    workers = max(1, page_workers)
    if workers == 1 or len(page_refs) <= 1:
        for page_ref in page_refs:
            yield _scan_page_for_export(
                page_ref=page_ref,
                data_files=data_files,
                table=table,
                external_lobs=external_lobs,
            )
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        yield from executor.map(
            lambda page_ref: _scan_page_for_export(
                page_ref=page_ref,
                data_files=data_files,
                table=table,
                external_lobs=external_lobs,
            ),
            page_refs,
        )


def _scan_page_for_export(
    *,
    page_ref: StoragePageRef,
    data_files: dict[int, DataFile],
    table: TableMeta,
    external_lobs: bool,
) -> PageScanResult:
    page_no = page_ref.page_no
    page_file = data_files[page_ref.file_no]
    page = page_file.read_page(page_no)
    if table.storage.storage_id is not None:
        header = ObservedPageHeader.from_page(page)
        if header.page_kind_raw != 0x14:
            return PageScanResult(
                page_ref=page_ref,
                accepted=False,
                pages_skipped_non_data=1,
            )
        if header.storage_id_candidate != table.storage.storage_id:
            return PageScanResult(
                page_ref=page_ref,
                accepted=False,
                pages_skipped_storage_mismatch=1,
            )
    physical_rows = scan_observed_row_chain(page)
    rows_skipped_deleted = sum(1 for row in physical_rows if row.is_deleted)
    rows = iter_observed_rows_by_slots(page) or [
        row for row in physical_rows if not row.is_deleted
    ]
    decoded_rows: list[tuple[Any, list[object]]] = []
    rows_skipped_decode_error = 0
    decode_errors: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        try:
            values = decode_observed_row_values(
                row,
                table.columns,
                external_lobs=external_lobs,
            )
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
                decode_errors.append(f"page={page_no} offset={row.page_offset}: {exc}")
            continue
        decoded_rows.append((row, values))
    return PageScanResult(
        page_ref=page_ref,
        accepted=True,
        rows=tuple(decoded_rows),
        rows_skipped_deleted=rows_skipped_deleted,
        rows_skipped_decode_error=rows_skipped_decode_error,
        decode_errors=tuple(decode_errors),
        diagnostics=tuple(diagnostics),
    )


class LobExportContext:
    def __init__(
        self,
        *,
        output: Path,
        options: LobExportOptions,
        data_files: dict[int, DataFile],
    ) -> None:
        if options.mode not in {"inline", "external"}:
            raise ValueError(f"unsupported LOB export mode: {options.mode}")
        self.output = output
        self.options = options
        self.data_files = data_files
        self.base_dir = options.directory or output.with_suffix(".lob")
        self.manifest_path = self.base_dir / options.manifest_name
        self._manifest = None

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()

    def write_lob(
        self,
        *,
        table: TableMeta,
        column: ColumnMeta,
        row_sequence: int,
        value: LobValue,
    ) -> LobWriteResult:
        self._ensure_manifest()
        row_dir = self.base_dir / f"{row_sequence:08d}"
        row_dir.mkdir(parents=True, exist_ok=True)
        extension = _lob_extension(value)
        file_path = row_dir / f"{_safe_lob_name(column.name)}.{extension}"
        status = "inline"
        page_numbers: tuple[int, ...] = ()
        source_encoding = value.source_encoding
        output_encoding = "utf-8" if value.source_encoding is not None else None
        source_bytes: int | None = None
        if value.inline_payload is None:
            try:
                lob_read = read_out_of_line_lob(
                    raw_locator=value.raw,
                    data_files=self.data_files,
                    group_id=table.storage.group_id,
                    file_no=table.storage.file_no,
                )
            except LobReadError:
                payload = value.raw.hex().encode("ascii")
                extension = "locator.hex"
                file_path = row_dir / f"{_safe_lob_name(column.name)}.{extension}"
                status = "unresolved-locator"
            else:
                status = "out-of-line"
                page_numbers = lob_read.page_numbers
                source_bytes = len(lob_read.payload)
                if value.type_name.upper() == "BLOB":
                    payload = lob_read.payload
                else:
                    text, source_encoding = decode_character_bytes_with_encoding(
                        lob_read.payload
                    )
                    output_encoding = "utf-8"
                    payload = text.encode("utf-8")
        elif value.type_name.upper() == "BLOB":
            payload = value.inline_payload
        else:
            text = value.text if value.text is not None else ""
            source_bytes = len(value.inline_payload)
            payload = text.encode("utf-8")
        file_path.write_bytes(payload)
        digest = _hash_payload(payload, self.options.hash_name)
        try:
            relative_path = file_path.relative_to(self.output.parent)
        except ValueError:
            relative_path = file_path
        manifest_row = {
            "table": table.qualified_name,
            "row_sequence": row_sequence,
            "column": column.name,
            "type_name": value.type_name,
            "status": status,
            "file": str(relative_path),
            "bytes": len(payload),
            self.options.hash_name: digest,
        }
        if source_encoding is not None:
            manifest_row["source_encoding"] = source_encoding
        if output_encoding is not None:
            manifest_row["output_encoding"] = output_encoding
        if source_bytes is not None and source_bytes != len(payload):
            manifest_row["source_bytes"] = source_bytes
        if page_numbers:
            manifest_row["pages"] = list(page_numbers)
        if status == "unresolved-locator":
            manifest_row["raw_locator_bytes"] = len(value.raw)
        assert self._manifest is not None
        self._manifest.write(json.dumps(manifest_row, sort_keys=True) + "\n")
        return LobWriteResult(placeholder=f"@LOB:{relative_path}", status=status)

    def _ensure_manifest(self) -> None:
        if self._manifest is not None:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._manifest = self.manifest_path.open("w", encoding="utf-8")


def _open_lob_export_context(
    *,
    output: Path,
    options: LobExportOptions,
    data_files: dict[int, DataFile],
) -> LobExportContext | None:
    if options.mode == "inline":
        return None
    return LobExportContext(output=output, options=options, data_files=data_files)


def _export_lob_values(
    *,
    values: list[object],
    table: TableMeta,
    row_sequence: int,
    context: LobExportContext | None,
    diagnostics: list[dict[str, Any]],
) -> list[object]:
    if context is None:
        return values
    exported: list[object] = []
    unresolved_count = 0
    for column, value in zip(table.columns, values):
        if not isinstance(value, LobValue):
            exported.append(value)
            continue
        result = context.write_lob(
            table=table,
            column=column,
            row_sequence=row_sequence,
            value=value,
        )
        exported.append(result.placeholder)
        if result.status == "unresolved-locator":
            unresolved_count += 1
    if unresolved_count and not any(
        item.get("code") == "lob-locator-not-followed" for item in diagnostics
    ):
        diagnostics.append(
            {
                "level": "error",
                "code": "lob-locator-not-followed",
                "message": "one or more out-of-line LOB locators were preserved but the LOB segment payload was not followed yet",
                "count": unresolved_count,
            }
        )
    return exported


def _write_row_archive_lobs(
    *,
    archive: RowArchiveWriter,
    values: list[object],
    table: TableMeta,
    data_files: dict[int, DataFile],
    row_sequence: int,
    diagnostics: list[dict[str, Any]],
) -> None:
    unresolved_count = 0
    for column, value in zip(table.columns, values):
        if not isinstance(value, LobValue):
            continue
        payload = _row_archive_lob_payload(
            value=value,
            table=table,
            data_files=data_files,
        )
        if payload is None:
            unresolved_count += 1
            continue
        archive.write_lob(
            row_sequence=row_sequence,
            column_name=column.name,
            type_name=value.type_name,
            payload=payload,
        )
    if unresolved_count and not any(
        item.get("code") == "lob-locator-not-followed" for item in diagnostics
    ):
        diagnostics.append(
            {
                "level": "error",
                "code": "lob-locator-not-followed",
                "message": "one or more out-of-line LOB locators were preserved but the LOB page payload was not embedded in the row archive",
                "count": unresolved_count,
            }
        )


def _row_archive_lob_payload(
    *,
    value: LobValue,
    table: TableMeta,
    data_files: dict[int, DataFile],
) -> bytes | None:
    if value.inline_payload is not None:
        if value.type_name.upper() == "BLOB":
            return value.inline_payload
        text = value.text if value.text is not None else ""
        return text.encode("utf-8")
    try:
        lob_read = read_out_of_line_lob(
            raw_locator=value.raw,
            data_files=data_files,
            group_id=table.storage.group_id,
            file_no=table.storage.file_no,
        )
    except LobReadError:
        return None
    if value.type_name.upper() == "BLOB":
        return lob_read.payload
    text, _encoding = decode_character_bytes_with_encoding(lob_read.payload)
    return text.encode("utf-8")


def _lob_extension(value: LobValue) -> str:
    type_name = value.type_name.upper()
    if type_name == "BLOB":
        return "blob"
    if type_name == "TEXT":
        return "text"
    return "clob"


def _safe_lob_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in value)


def _hash_payload(payload: bytes, name: str) -> str:
    if name != "sha256":
        raise ValueError(f"unsupported LOB hash: {name}")
    return hashlib.sha256(payload).hexdigest()


def _emit_extract_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if progress is not None:
        progress(event)


def _should_report_page_progress(
    *,
    pages_done: int,
    pages_total: int,
    progress_interval_pages: int,
) -> bool:
    if pages_total <= 0:
        return False
    interval = max(1, progress_interval_pages)
    return pages_done == pages_total or pages_done % interval == 0


def _create_table_sql(table: TableMeta) -> str:
    owner_prefix = f"{table.owner}." if table.owner else ""
    column_lines = []
    for column in table.columns:
        column_lines.append(f"  {column.name} {_ddl_type(column)}")
    columns_sql = ",\n".join(column_lines)
    return f"CREATE TABLE {owner_prefix}{table.name} (\n{columns_sql}\n);"


def _ddl_type(column: ColumnMeta) -> str:
    type_name = column.type_name.upper()
    if (
        column.length is not None
        and type_name in {"DEC", "DECIMAL", "NUMBER", "NUMERIC"}
    ):
        if column.scale is not None and column.scale > 0:
            return f"{type_name}({column.length},{column.scale})"
        return f"{type_name}({column.length})"
    if (
        column.scale is not None
        and column.scale > 0
        and type_name in {"TIME", "TIMESTAMP", "DATETIME"}
    ):
        return f"{type_name}({column.scale})"
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
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> PagePlan:
    if table.storage.page_refs:
        return _walk_leaf_chain(
            data_files=data_files,
            start_pages=table.storage.page_refs,
            mode="segment-manifest-page-ref-walk",
        )
    if table.storage.page_numbers:
        return _walk_same_file_leaf_chain(
            data_file=data_files[table.storage.file_no],
            file_no=table.storage.file_no,
            start_pages=table.storage.page_numbers,
            mode="segment-manifest-page-ref-walk",
        )
    if table.storage.storage_id is not None:
        root_plan = _build_root_page_plan(table=table, data_files=data_files)
        if root_plan.pages or any(item.get("level") == "error" for item in root_plan.diagnostics):
            return root_plan
        storage_scan_plan = _build_storage_id_page_plan(table=table, data_files=data_files)
        if storage_scan_plan.pages:
            return PagePlan(
                pages=storage_scan_plan.pages,
                diagnostics=root_plan.diagnostics + storage_scan_plan.diagnostics,
                mode=storage_scan_plan.mode,
            )
        global_scan_plan = _build_storage_id_global_page_plan(
            table=table,
            data_files=data_files,
            progress=progress,
        )
        if global_scan_plan.pages:
            return PagePlan(
                pages=global_scan_plan.pages,
                diagnostics=root_plan.diagnostics + global_scan_plan.diagnostics,
                mode=global_scan_plan.mode,
            )
        return PagePlan(
            pages=(),
            diagnostics=root_plan.diagnostics + storage_scan_plan.diagnostics + global_scan_plan.diagnostics,
            mode=global_scan_plan.mode,
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
        mode="calibrated-metadata-page-range-scan",
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
            mode="page-plan-error",
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
            mode="page-plan-error",
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
            mode="btree-root-plan-unavailable",
        )
    if root_header.page_kind_raw == 0x14:
        plan = _walk_same_file_leaf_chain(
            data_file=data_file,
            file_no=table.storage.file_no,
            start_pages=(root_page_no,),
            mode="btree-root-leaf-chain",
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
            mode=plan.mode,
        )
    if root_header.page_kind_raw == 0x15:
        descent = _btree_find_leftmost_leaf(
            data_file=data_file,
            start_page=root_page_no,
            storage_id=table.storage.storage_id,
            pages_total=pages_total,
        )
        entry_children = _btree_root_entry_child_pages(root_page, root_header.observed_row_count)
        if descent.leaf_page is None:
            return PagePlan(
                pages=(),
                diagnostics=(
                    {
                        "level": "warning",
                        "code": "page-plan-btree-root-no-leftmost-child",
                        "message": "BTREE root/internal pages did not contain a usable path to a leaf page; storage-id scan fallback is required",
                        "root_page": root_page_no,
                        "storage_id": table.storage.storage_id,
                        "entry_count": root_header.observed_row_count,
                        "descent_pages": descent.pages,
                        "stop_reason": descent.stop_reason,
                    },
                ),
                mode="btree-root-plan-unavailable",
            )
        plan = _walk_same_file_leaf_chain(
            data_file=data_file,
            file_no=table.storage.file_no,
            start_pages=(descent.leaf_page,),
            mode="btree-internal-descent",
        )
        planned_pages = tuple(page_ref.page_no for page_ref in plan.pages)
        diagnostics: list[dict[str, Any]] = [
            {
                "level": "info",
                "code": "page-plan-btree-internal-descent",
                "message": "planned BTREE data pages by descending root/internal pages to the leftmost leaf and walking the leaf next-chain",
                "root_page": root_page_no,
                "storage_id": table.storage.storage_id,
                "leftmost_leaf_page": descent.leaf_page,
                "descent_pages": descent.pages,
                "root_entry_count": root_header.observed_row_count,
                "root_entry_child_pages": entry_children,
                "pages_planned": len(plan.pages),
            }
        ]
        planned_set = set(planned_pages)
        uncovered_entry_children = _btree_uncovered_entry_children(
            data_file=data_file,
            entry_children=entry_children,
            planned_pages=planned_set,
            descent_pages=set(descent.pages),
            storage_id=table.storage.storage_id,
            pages_total=pages_total,
        )
        if uncovered_entry_children:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "page-plan-btree-root-entry-mismatch",
                    "message": "BTREE root child entries were not all covered by the descent path or walked leaf chain",
                    "root_page": root_page_no,
                    "entry_child_pages": entry_children,
                    "uncovered_entry_child_pages": uncovered_entry_children,
                    "walked_pages": list(planned_pages),
                }
            )
        diagnostics.extend(plan.diagnostics)
        return PagePlan(pages=plan.pages, diagnostics=tuple(diagnostics), mode=plan.mode)
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
        mode="btree-root-plan-unavailable",
    )


@dataclass(frozen=True)
class BTreeDescent:
    leaf_page: int | None
    pages: tuple[int, ...]
    stop_reason: str


def _btree_find_leftmost_leaf(
    *,
    data_file: DataFile,
    start_page: int,
    storage_id: int | None,
    pages_total: int,
) -> BTreeDescent:
    descent = _btree_descend_leftmost_leaf(
        data_file=data_file,
        start_page=start_page,
        storage_id=storage_id,
        pages_total=pages_total,
    )
    if descent.leaf_page is not None:
        return descent

    # If the first internal page path cannot be followed, stay within the
    # table BTree structure by trying internal siblings linked from the
    # visited internal pages before any broader fallback is considered.
    visited = list(descent.pages)
    for sibling_page in _btree_internal_sibling_pages(
        data_file=data_file,
        start_pages=descent.pages,
        storage_id=storage_id,
        pages_total=pages_total,
    ):
        if sibling_page in visited:
            continue
        sibling_descent = _btree_descend_leftmost_leaf(
            data_file=data_file,
            start_page=sibling_page,
            storage_id=storage_id,
            pages_total=pages_total,
        )
        combined_pages = tuple(dict.fromkeys(tuple(visited) + sibling_descent.pages))
        if sibling_descent.leaf_page is not None:
            return BTreeDescent(sibling_descent.leaf_page, combined_pages, sibling_descent.stop_reason)
        visited = list(combined_pages)
    return BTreeDescent(None, tuple(visited), descent.stop_reason)


def _btree_internal_sibling_pages(
    *,
    data_file: DataFile,
    start_pages: tuple[int, ...],
    storage_id: int | None,
    pages_total: int,
) -> tuple[int, ...]:
    siblings: list[int] = []
    seen: set[int] = set(start_pages)
    for start_page in start_pages:
        if start_page < 0 or start_page >= pages_total:
            continue
        page = data_file.read_page(start_page)
        header = ObservedPageHeader.from_page(page)
        if header.page_kind_raw != 0x15:
            continue
        next_page = _page_ref_page_no(page, 0x0E)
        while next_page is not None:
            if next_page < 0 or next_page >= pages_total or next_page in seen:
                break
            sibling = data_file.read_page(next_page)
            sibling_header = ObservedPageHeader.from_page(sibling)
            if sibling_header.page_no != next_page:
                break
            if storage_id is not None and sibling_header.storage_id_candidate != storage_id:
                break
            if sibling_header.page_kind_raw != 0x15:
                break
            siblings.append(next_page)
            seen.add(next_page)
            next_page = _page_ref_page_no(sibling, 0x0E)
    return tuple(siblings)


def _btree_descend_leftmost_leaf(
    *,
    data_file: DataFile,
    start_page: int,
    storage_id: int | None,
    pages_total: int,
) -> BTreeDescent:
    current_page = start_page
    visited: list[int] = []
    seen: set[int] = set()
    while True:
        if current_page < 0 or current_page >= pages_total:
            return BTreeDescent(None, tuple(visited), "page-out-of-range")
        if current_page in seen:
            return BTreeDescent(None, tuple(visited), "cycle-detected")
        seen.add(current_page)
        visited.append(current_page)
        page = data_file.read_page(current_page)
        header = ObservedPageHeader.from_page(page)
        if header.page_no != current_page:
            return BTreeDescent(None, tuple(visited), "page-identity-mismatch")
        if storage_id is not None and header.storage_id_candidate != storage_id:
            return BTreeDescent(None, tuple(visited), "storage-id-mismatch")
        if header.page_kind_raw == 0x14:
            return BTreeDescent(current_page, tuple(visited), "leaf")
        if header.page_kind_raw != 0x15:
            return BTreeDescent(None, tuple(visited), f"unexpected-kind-0x{header.page_kind_raw:x}")
        child_page = _btree_root_leftmost_child(page)
        if child_page is None:
            return BTreeDescent(None, tuple(visited), "leftmost-child-missing")
        current_page = child_page


def _page_ref_page_no(page: bytes, offset: int) -> int | None:
    if offset < 0 or offset + 6 > len(page):
        return None
    raw = page[offset : offset + 6]
    if raw == b"\xff" * 6:
        return None
    return int.from_bytes(raw[2:6], "little")


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


def _btree_uncovered_entry_children(
    *,
    data_file: DataFile,
    entry_children: list[int],
    planned_pages: set[int],
    descent_pages: set[int],
    storage_id: int | None,
    pages_total: int,
) -> list[int]:
    uncovered: list[int] = []
    for child_page in entry_children:
        if _btree_child_is_covered(
            data_file=data_file,
            child_page=child_page,
            planned_pages=planned_pages,
            descent_pages=descent_pages,
            storage_id=storage_id,
            pages_total=pages_total,
            seen=set(),
        ):
            continue
        uncovered.append(child_page)
    return uncovered


def _btree_child_is_covered(
    *,
    data_file: DataFile,
    child_page: int,
    planned_pages: set[int],
    descent_pages: set[int],
    storage_id: int | None,
    pages_total: int,
    seen: set[int],
) -> bool:
    if child_page in planned_pages or child_page in descent_pages:
        return True
    if child_page in seen:
        return False
    if child_page < 0 or child_page >= pages_total:
        return False
    seen.add(child_page)
    page = data_file.read_page(child_page)
    header = ObservedPageHeader.from_page(page)
    if header.page_no != child_page:
        return False
    if storage_id is not None and header.storage_id_candidate != storage_id:
        return False
    if header.page_kind_raw != 0x15:
        return False

    descent = _btree_descend_leftmost_leaf(
        data_file=data_file,
        start_page=child_page,
        storage_id=storage_id,
        pages_total=pages_total,
    )
    if descent.leaf_page in planned_pages:
        return True

    entry_children = _btree_root_entry_child_pages(page, header.observed_row_count)
    return bool(entry_children) and all(
        _btree_child_is_covered(
            data_file=data_file,
            child_page=entry_child,
            planned_pages=planned_pages,
            descent_pages=descent_pages,
            storage_id=storage_id,
            pages_total=pages_total,
            seen=seen,
        )
        for entry_child in entry_children
    )


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
            mode="page-plan-error",
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
    return PagePlan(
        pages=tuple(planned),
        diagnostics=tuple(diagnostics),
        mode="storage-id-window-scan",
    )


def _build_storage_id_global_page_plan(
    *,
    table: TableMeta,
    data_files: dict[int, DataFile],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> PagePlan:
    if table.storage.storage_id is None:
        return PagePlan(pages=(), diagnostics=(), mode="storage-id-global-scan")
    planned: list[StoragePageRef] = []
    files_scanned = 0
    header_hits = 0
    skipped_non_data = 0
    skipped_identity_mismatch = 0
    for file_no, data_file in sorted(data_files.items()):
        page_size = data_file.page_size
        path = data_file.path
        pages_total = path.stat().st_size // page_size
        if pages_total <= 0:
            continue
        files_scanned += 1
        file_header_hits_before = header_hits
        file_pages_before = len(planned)
        _emit_extract_progress(
            progress,
            {
                "event": "storage_scan_file_start",
                "table": table.qualified_name,
                "file_no": file_no,
                "path": str(path),
                "pages_total": pages_total,
                "storage_id": table.storage.storage_id,
            },
        )
        with path.open("rb") as file:
            with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for page_no in range(pages_total):
                    page_start = page_no * page_size
                    if int.from_bytes(mm[page_start + 0x3A : page_start + 0x3E], "little") != table.storage.storage_id:
                        if _should_report_storage_scan_progress(page_no + 1, pages_total):
                            _emit_storage_scan_progress(
                                progress=progress,
                                table=table,
                                file_no=file_no,
                                pages_scanned=page_no + 1,
                                pages_total=pages_total,
                                header_hits=header_hits - file_header_hits_before,
                                pages_planned=len(planned) - file_pages_before,
                            )
                        continue
                    header_page_no = int.from_bytes(mm[page_start + 4 : page_start + 8], "little")
                    header_file_no = int.from_bytes(mm[page_start : page_start + 4], "little") >> 16
                    page_kind = int.from_bytes(mm[page_start + 0x14 : page_start + 0x18], "little")
                    header_hits += 1
                    if header_file_no != file_no or header_page_no != page_no:
                        skipped_identity_mismatch += 1
                    elif page_kind != 0x14:
                        skipped_non_data += 1
                    else:
                        planned.append(StoragePageRef(file_no=file_no, page_no=page_no))
                    if _should_report_storage_scan_progress(page_no + 1, pages_total):
                        _emit_storage_scan_progress(
                            progress=progress,
                            table=table,
                            file_no=file_no,
                            pages_scanned=page_no + 1,
                            pages_total=pages_total,
                            header_hits=header_hits - file_header_hits_before,
                            pages_planned=len(planned) - file_pages_before,
                        )
        _emit_extract_progress(
            progress,
            {
                "event": "storage_scan_file_done",
                "table": table.qualified_name,
                "file_no": file_no,
                "path": str(path),
                "header_hits": header_hits - file_header_hits_before,
                "pages_planned": len(planned) - file_pages_before,
                "pages_planned_total": len(planned),
                "storage_id": table.storage.storage_id,
            },
        )
    diagnostics: list[dict[str, Any]] = []
    if planned:
        diagnostics.append(
            {
                "level": "info",
                "code": "page-plan-storage-id-global-scan",
                "message": "planned BTREE data pages by scanning DBF page headers for the table storage id",
                "storage_id": table.storage.storage_id,
                "files_scanned": files_scanned,
                "header_hits": header_hits,
                "pages_planned": len(planned),
                "skipped_non_data": skipped_non_data,
                "skipped_identity_mismatch": skipped_identity_mismatch,
            }
        )
    else:
        diagnostics.append(
            {
                "level": "error",
                "code": "page-plan-storage-id-global-no-data-pages",
                "message": "no BTREE data pages matched the table storage id in any same-group data file",
                "storage_id": table.storage.storage_id,
                "files_scanned": files_scanned,
                "header_hits": header_hits,
                "skipped_non_data": skipped_non_data,
                "skipped_identity_mismatch": skipped_identity_mismatch,
            }
        )
    return PagePlan(
        pages=tuple(planned),
        diagnostics=tuple(diagnostics),
        mode="storage-id-global-scan",
    )


def _build_orphan_storage_id_page_plan(
    *,
    table: TableMeta,
    data_files: dict[int, DataFile],
    storage_ids: tuple[int, ...],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> PagePlan:
    if not storage_ids:
        raise ValueError("orphan storage scan requires at least one storage id")
    pages: list[StoragePageRef] = []
    seen_pages: set[tuple[int, int]] = set()
    diagnostics: list[dict[str, Any]] = [
        {
            "level": "warning",
            "code": "page-plan-orphan-storage-id-scan",
            "message": "planned pages by explicitly scanning for an old/orphan storage id; use only for truncate/drop recovery when current segment metadata no longer owns all pages",
            "storage_ids": list(storage_ids),
        }
    ]
    for storage_id in storage_ids:
        scan_table = replace(
            table,
            storage=replace(table.storage, storage_id=storage_id),
        )
        plan = _build_storage_id_global_page_plan(
            table=scan_table,
            data_files=data_files,
            progress=progress,
        )
        diagnostics.extend(plan.diagnostics)
        for page in plan.pages:
            key = (page.file_no, page.page_no)
            if key in seen_pages:
                continue
            seen_pages.add(key)
            pages.append(page)
    return PagePlan(
        pages=tuple(pages),
        diagnostics=tuple(diagnostics),
        mode="orphan-storage-id-global-scan",
    )


def _normalize_orphan_storage_ids(
    *,
    orphan_scan_storage_id: int | None,
    orphan_scan_storage_ids: tuple[int, ...],
) -> tuple[int, ...]:
    values: list[int] = []
    if orphan_scan_storage_id is not None:
        values.append(orphan_scan_storage_id)
    values.extend(orphan_scan_storage_ids)
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value <= 0:
            raise ValueError("orphan storage ids must be positive integers")
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)



def _should_report_storage_scan_progress(pages_scanned: int, pages_total: int) -> bool:
    return pages_scanned == pages_total or pages_scanned % STORAGE_SCAN_PROGRESS_PAGES == 0


def _emit_storage_scan_progress(
    *,
    progress: Callable[[dict[str, Any]], None] | None,
    table: TableMeta,
    file_no: int,
    pages_scanned: int,
    pages_total: int,
    header_hits: int,
    pages_planned: int,
) -> None:
    _emit_extract_progress(
        progress,
        {
            "event": "storage_scan_progress",
            "table": table.qualified_name,
            "file_no": file_no,
            "pages_scanned": pages_scanned,
            "pages_total": pages_total,
            "header_hits": header_hits,
            "pages_planned": pages_planned,
        },
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
    mode: str = "leaf-chain-walk",
) -> PagePlan:
    return _walk_leaf_chain(
        data_files={file_no: data_file},
        start_pages=tuple(
            StoragePageRef(file_no=file_no, page_no=page_no)
            for page_no in start_pages
        ),
        mode=mode,
    )


def _walk_leaf_chain(
    *,
    data_files: dict[int, DataFile],
    start_pages: tuple[StoragePageRef, ...],
    mode: str = "leaf-chain-walk",
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
                if key in start_keys and header.page_kind_raw == 0x15:
                    descent = _btree_find_leftmost_leaf(
                        data_file=data_file,
                        start_page=page_no,
                        storage_id=header.storage_id_candidate,
                        pages_total=pages_total,
                    )
                    if descent.leaf_page is not None:
                        diagnostics.append(
                            {
                                "level": "info",
                                "code": "page-plan-start-internal-descent",
                                "message": "planned start page was a BTREE internal/root page; descended to the leftmost leaf page",
                                "file_no": file_no,
                                "root_page": page_no,
                                "leaf_page": descent.leaf_page,
                                "storage_id": header.storage_id_candidate,
                            }
                        )
                        page_ref = StoragePageRef(file_no=file_no, page_no=descent.leaf_page)
                        continue
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
    return PagePlan(pages=tuple(result), diagnostics=tuple(diagnostics), mode=mode)
