from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .discovery import DiscoveredDataFile, discover_data_files
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

    files = discover_data_files(database_dir, page_size=page_size)
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

    return {
        "database_dir": str(database_dir),
        "page_size": page_size,
        "files_total": len(files),
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
        ),
        "diagnostics": _summary_diagnostics(file_entries),
        "duplicate_file_hints": duplicate_file_hints,
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
) -> list[str]:
    warnings: list[str] = []
    if not system_candidates:
        warnings.append("SYSTEM.DBF candidate not found")
    elif len(system_candidates) > 1:
        warnings.append("multiple SYSTEM.DBF candidates found")
    if duplicate_file_hints:
        warnings.append("duplicate group/file_no_hint combinations found")
    if any(item["diagnostics"] for item in file_entries):
        warnings.append("one or more files have diagnostics")
    return warnings


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


def _summary_diagnostics(file_entries: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    files_with_diagnostics = 0
    for item in file_entries:
        diagnostics = item.get("diagnostics", [])
        if diagnostics:
            files_with_diagnostics += 1
        for diagnostic in diagnostics:
            counts[str(diagnostic["code"])] += 1
    return {
        "files_with_diagnostics": files_with_diagnostics,
        "counts_by_code": dict(sorted(counts.items())),
    }
