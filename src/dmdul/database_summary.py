from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .control_file import summarize_control_file
from .discovery import (
    DiscoveredDataFile,
    discover_data_files,
    find_control_files,
    find_dbf_files,
)
from .page import observed_page_kind_label
from .page_catalog import catalog_data_file_pages


def summarize_database_dir(
    *,
    database_dir: Path,
    page_size: int = 8192,
    catalog_pages: int = 64,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Summarize a DM database directory for storage exploration."""

    dbf_paths = find_dbf_files(database_dir)
    control_paths = find_control_files(database_dir)
    control_files = [
        summarize_control_file(path, sample_limit=sample_limit)
        for path in control_paths
    ]
    files = discover_data_files(database_dir, page_size=page_size)
    discovered_paths = {item.path for item in files}
    skipped_files = [
        _skipped_file_entry(path, page_size=page_size)
        for path in dbf_paths
        if path not in discovered_paths
    ]
    groups: dict[int, list[DiscoveredDataFile]] = defaultdict(list)
    for item in files:
        groups[item.group_id].append(item)

    file_entries = [
        _file_entry(item, catalog_pages=catalog_pages, sample_limit=sample_limit)
        for item in files
    ]
    system_candidates = [
        str(item.path) for item in files if item.is_system_candidate
    ]
    duplicate_file_hints = _duplicate_file_hints(groups)
    control_file_data_files = _control_file_data_file_manifest(
        control_files=control_files,
        dbf_paths=dbf_paths,
        discovered_files=files,
    )
    control_file_dbf_hints = _control_file_dbf_hints(
        control_file_data_files=control_file_data_files,
    )
    summary_level_diagnostics = _summary_level_diagnostics(
        control_files=control_files,
        duplicate_file_hints=duplicate_file_hints,
        control_file_dbf_hints=control_file_dbf_hints,
    )

    return {
        "database_dir": str(database_dir),
        "page_size": page_size,
        "dbf_files_total": len(dbf_paths),
        "control_files_total": len(control_files),
        "files_total": len(files),
        "skipped_files_total": len(skipped_files),
        "groups": [
            {
                "group_id": group_id,
                "files": len(group_files),
                "file_no_hints": [item.file_no_hint for item in group_files],
                "paths": [str(item.path) for item in group_files],
            }
            for group_id, group_files in sorted(groups.items())
        ],
        "system_candidates": system_candidates,
        "warnings": _summary_warnings(
            system_candidates=system_candidates,
            duplicate_file_hints=duplicate_file_hints,
            file_entries=file_entries,
            skipped_files=skipped_files,
            control_files=control_files,
            control_file_dbf_hints=control_file_dbf_hints,
        ),
        "diagnostics": _summary_diagnostics(
            file_entries,
            skipped_files,
            summary_level_diagnostics,
        ),
        "summary_diagnostics": summary_level_diagnostics,
        "duplicate_file_hints": duplicate_file_hints,
        "control_file_data_files": control_file_data_files,
        "control_file_dbf_hints": control_file_dbf_hints,
        "control_files": control_files,
        "skipped_files": skipped_files,
        "files": file_entries,
    }


def _file_entry(
    item: DiscoveredDataFile,
    *,
    catalog_pages: int,
    sample_limit: int,
) -> dict[str, Any]:
    diagnostics = _file_diagnostics(item)
    entry: dict[str, Any] = {
        "path": str(item.path),
        "bytes": item.bytes,
        "page_size": item.page_size,
        "pages": item.pages,
        "trailing_bytes": item.bytes % item.page_size,
        "group_raw": item.group_raw,
        "page_type_raw": item.page_type_raw,
        "group_id": item.group_id,
        "file_no_hint": item.file_no_hint,
        "page0_page_no": item.page_no,
        "page0_kind_raw": item.page_kind_raw,
        "page0_kind_label": observed_page_kind_label(item.page_kind_raw),
        "system_candidate": item.is_system_candidate,
        "diagnostics": diagnostics,
    }
    if catalog_pages > 0:
        catalog = catalog_data_file_pages(
            path=item.path,
            page_size=item.page_size,
            max_pages=catalog_pages,
            sample_limit=sample_limit,
        )
        entry["catalog_sample"] = {
            "scanned_pages": catalog["scan"]["scanned_pages"],
            "zero_pages": catalog["zero_pages"],
            "nonzero_pages": catalog["nonzero_pages"],
            "page_kind_counts": catalog["page_kind_counts"],
            "page_no_mismatches": catalog["page_no_mismatches"],
            "reference_out_of_range": catalog["reference_out_of_range"],
        }
        if catalog["page_no_mismatches"]:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "catalog-page-number-mismatch",
                    "message": "sampled pages contain header page numbers that differ from physical page numbers",
                    "count": len(catalog["page_no_mismatches"]),
                }
            )
        if catalog["reference_out_of_range"]:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "catalog-reference-out-of-range",
                    "message": "sampled pages contain same-file previous/next references beyond the file page count",
                    "count": len(catalog["reference_out_of_range"]),
                }
            )
    return entry


def _duplicate_file_hints(
    groups: dict[int, list[DiscoveredDataFile]],
) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    for group_id, group_files in sorted(groups.items()):
        by_hint: dict[tuple[int, int], list[DiscoveredDataFile]] = defaultdict(list)
        for item in group_files:
            by_hint[(item.file_no_hint, item.page_kind_raw)].append(item)
        for (file_no_hint, page_kind_raw), files in sorted(by_hint.items()):
            if len(files) > 1:
                duplicates.append(
                    {
                        "group_id": group_id,
                        "file_no_hint": file_no_hint,
                        "page0_kind_raw": page_kind_raw,
                        "page0_kind_label": observed_page_kind_label(page_kind_raw),
                        "paths": [str(item.path) for item in files],
                    }
                )
    return duplicates


def _summary_warnings(
    *,
    system_candidates: list[str],
    duplicate_file_hints: list[dict[str, Any]],
    file_entries: list[dict[str, Any]],
    skipped_files: list[dict[str, Any]],
    control_files: list[dict[str, Any]],
    control_file_dbf_hints: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if not control_files:
        warnings.append("dm.ctl/control file not found")
    elif control_file_dbf_hints["unmatched_hints"]:
        warnings.append("one or more DBF path hints from control files were not found")
    elif control_file_dbf_hints["ambiguous_hints"]:
        warnings.append("one or more DBF path hints from control files matched multiple files")
    if not system_candidates:
        warnings.append("SYSTEM.DBF candidate not found")
    elif len(system_candidates) > 1:
        warnings.append("multiple SYSTEM.DBF candidates found")
    if duplicate_file_hints:
        warnings.append("duplicate group/file_no_hint combinations found")
    if any(item["diagnostics"] for item in file_entries):
        warnings.append("one or more files have diagnostics")
    if skipped_files:
        warnings.append("one or more DBF files were skipped")
    return warnings


def _control_file_data_file_manifest(
    *,
    control_files: list[dict[str, Any]],
    dbf_paths: list[Path],
    discovered_files: list[DiscoveredDataFile],
) -> dict[str, Any]:
    by_basename: dict[str, list[Path]] = defaultdict(list)
    for path in dbf_paths:
        by_basename[path.name.lower()].append(path)

    discovered_by_path = {item.path: item for item in discovered_files}

    entries: list[dict[str, Any]] = []
    for control_file in control_files:
        records = control_file.get("dbf_path_occurrences") or control_file.get(
            "dbf_path_hint_records",
            [],
        )
        for record in records:
            if not isinstance(record, dict):
                continue
            text = str(record.get("text", ""))
            basename = str(
                record.get("basename") or Path(text.replace("\\", "/")).name.lower()
            )
            matches = by_basename.get(basename, [])
            entries.append(
                {
                    "control_file": control_file.get("path"),
                    "control_file_ordinal": record.get("ordinal"),
                    "text": text,
                    "normalized_path": record.get("normalized_path"),
                    "basename": basename,
                    "offset": record.get("offset"),
                    "matched_paths": [str(path) for path in matches],
                    "matched": bool(matches),
                    "observed_files": [
                        _control_file_observed_file_entry(discovered_by_path[path])
                        for path in matches
                        if path in discovered_by_path
                    ],
                }
            )
    return {
        "entries_total": len(entries),
        "entries": entries,
        "matched_entries": [item for item in entries if item["matched"]],
        "unmatched_entries": [item for item in entries if not item["matched"]],
        "ambiguous_entries": [
            item for item in entries if len(item["matched_paths"]) > 1
        ],
    }


def _control_file_dbf_hints(
    *,
    control_file_data_files: dict[str, Any],
) -> dict[str, Any]:
    """Backward-compatible view of control-file DBF path matches."""

    return {
        "hints_total": control_file_data_files["entries_total"],
        "matched_hints": control_file_data_files["matched_entries"],
        "unmatched_hints": control_file_data_files["unmatched_entries"],
        "ambiguous_hints": control_file_data_files["ambiguous_entries"],
    }


def _control_file_observed_file_entry(item: DiscoveredDataFile) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "bytes": item.bytes,
        "page_size": item.page_size,
        "pages": item.pages,
        "group_raw": item.group_raw,
        "page_type_raw": item.page_type_raw,
        "group_id": item.group_id,
        "file_no_hint": item.file_no_hint,
        "page0_kind_raw": item.page_kind_raw,
        "page0_kind_label": observed_page_kind_label(item.page_kind_raw),
    }


def _summary_level_diagnostics(
    *,
    control_files: list[dict[str, Any]],
    duplicate_file_hints: list[dict[str, Any]],
    control_file_dbf_hints: dict[str, Any],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not control_files:
        diagnostics.append(
            {
                "level": "warning",
                "code": "control-file-not-found",
                "message": "dm.ctl/control file was not found in the database directory",
            }
        )
    unmatched_hints = control_file_dbf_hints["unmatched_hints"]
    if unmatched_hints:
        diagnostics.append(
            {
                "level": "error",
                "code": "control-file-dbf-hint-missing",
                "message": "one or more DBF path hints from control files were not found in the copied directory",
                "count": len(unmatched_hints),
            }
        )
    ambiguous_hints = control_file_dbf_hints["ambiguous_hints"]
    if ambiguous_hints:
        diagnostics.append(
            {
                "level": "warning",
                "code": "control-file-dbf-hint-ambiguous",
                "message": "one or more DBF path hints from control files matched multiple copied files",
                "count": len(ambiguous_hints),
            }
        )
    if duplicate_file_hints:
        diagnostics.append(
            {
                "level": "warning",
                "code": "duplicate-group-file-hint",
                "message": "multiple DBF files share the same observed group/file hint and page0 kind",
                "count": len(duplicate_file_hints),
            }
        )
    return diagnostics


def _file_diagnostics(item: DiscoveredDataFile) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    trailing_bytes = item.bytes % item.page_size
    if trailing_bytes:
        diagnostics.append(
            {
                "level": "error",
                "code": "trailing-bytes",
                "message": "file size is not an exact multiple of page size",
                "trailing_bytes": trailing_bytes,
            }
        )
    if item.page_no != 0:
        diagnostics.append(
            {
                "level": "warning",
                "code": "page0-header-page-number",
                "message": "page 0 header page number is not zero",
                "header_page_no": item.page_no,
            }
        )
    if item.pages == 0:
        diagnostics.append(
            {
                "level": "error",
                "code": "empty-data-file",
                "message": "file contains no complete pages",
            }
        )
    return diagnostics


def _summary_diagnostics(
    file_entries: list[dict[str, Any]],
    skipped_files: list[dict[str, Any]],
    summary_level_diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    files_with_diagnostics = 0
    for diagnostic in summary_level_diagnostics:
        counts[str(diagnostic["code"])] += 1
    for item in file_entries:
        diagnostics = item.get("diagnostics", [])
        if diagnostics:
            files_with_diagnostics += 1
        for diagnostic in diagnostics:
            counts[str(diagnostic["code"])] += 1
    for item in skipped_files:
        counts[str(item["code"])] += 1
    return {
        "files_with_diagnostics": files_with_diagnostics,
        "skipped_files": len(skipped_files),
        "counts_by_code": dict(sorted(counts.items())),
    }


def _skipped_file_entry(path: Path, *, page_size: int) -> dict[str, Any]:
    stat = path.stat()
    if stat.st_size < page_size:
        code = "short-dbf-file"
        message = "DBF file is smaller than one page"
    else:
        code = "unparsed-dbf-file"
        message = "DBF file was not parsed as a DM data file"
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "page_size": page_size,
        "code": code,
        "message": message,
    }
