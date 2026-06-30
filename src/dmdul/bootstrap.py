from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database_summary import summarize_database_dir
from .resolver import OfflineResolveError, resolve_offline_table_metadata


DICT_FILENAMES = ("file.dict", "user.dict", "tab.dict", "col.dict")


def build_bootstrap_dicts(
    *,
    database_dir: Path,
    output_dir: Path,
    page_size: int = 8192,
    catalog_pages: int = 0,
    sample_limit: int = 8,
    tables: tuple[str, ...] = (),
    owner: str | None = None,
    scan_pages: int = 64,
) -> dict[str, Any]:
    """Write first-stage bootstrap dictionary artifacts.

    The current implementation can build `file.dict` from `dm.ctl` evidence and
    DBF page-0 headers. User/table/column dictionaries are created as explicit
    empty artifacts until SYSTEM dictionary rows are decoded without heuristics.
    """

    summary = summarize_database_dir(
        database_dir=database_dir,
        page_size=page_size,
        catalog_pages=catalog_pages,
        sample_limit=sample_limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    dict_paths = {name: output_dir / name for name in DICT_FILENAMES}
    file_rows = _file_dict_rows(summary)
    user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_for_tables(
        database_dir=database_dir,
        tables=tables,
        owner=owner,
        page_size=page_size,
        scan_pages=scan_pages,
    )
    _write_jsonl(dict_paths["file.dict"], file_rows)
    _write_jsonl(dict_paths["user.dict"], user_rows)
    _write_jsonl(dict_paths["tab.dict"], table_rows)
    _write_jsonl(dict_paths["col.dict"], column_rows)

    manifest = {
        "mode": "dm-bootstrap-dicts",
        "database_dir": str(database_dir),
        "page_size": page_size,
        "dict_files": {name: str(path) for name, path in dict_paths.items()},
        "rows": {
            "file.dict": len(file_rows),
            "user.dict": len(user_rows),
            "tab.dict": len(table_rows),
            "col.dict": len(column_rows),
        },
        "steps": [
            {
                "step": 1,
                "name": "read-control-file-and-data-files",
                "status": "ok" if file_rows else "incomplete",
                "output": "file.dict",
            },
            {
                "step": 2,
                "name": "locate-system-dictionary-tables",
                "status": "heuristic-only",
                "output": None,
            },
            {
                "step": 3,
                "name": "dump-user-table-column-dictionaries",
                "status": _dictionary_dump_status(
                    requested_tables=tables,
                    table_rows=table_rows,
                    diagnostics=table_diagnostics,
                ),
                "output": ["user.dict", "tab.dict", "col.dict"],
            },
        ],
        "requested_tables": list(tables),
        "diagnostics": _bootstrap_diagnostics(
            summary,
            file_rows,
            requested_tables=tables,
            table_diagnostics=table_diagnostics,
        ),
        "database_summary": summary,
    }
    manifest_path = output_dir / "bootstrap_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _file_dict_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal, item in enumerate(summary.get("files", ()), start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "dict_type": "file",
                "ordinal": ordinal,
                "path": item.get("path"),
                "basename": Path(str(item.get("path", ""))).name,
                "bytes": item.get("bytes"),
                "page_size": item.get("page_size"),
                "pages": item.get("pages"),
                "group_id": item.get("group_id"),
                "file_no": item.get("file_no_hint"),
                "page_type_raw": item.get("page_type_raw"),
                "page0_kind_raw": item.get("page0_kind_raw"),
                "page0_kind_label": item.get("page0_kind_label"),
                "system_candidate": item.get("system_candidate"),
                "control_file_entries": _matched_control_entries(summary, item),
            }
        )
    return rows


def _dictionary_rows_for_tables(
    *,
    database_dir: Path,
    tables: tuple[str, ...],
    owner: str | None,
    page_size: int,
    scan_pages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    user_by_owner: dict[str, dict[str, Any]] = {}
    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for table_name in tables:
        try:
            resolution = resolve_offline_table_metadata(
                database_dir=database_dir,
                table_name=table_name,
                page_size=page_size,
                owner=owner,
                scan_pages=scan_pages,
            )
        except OfflineResolveError as exc:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "bootstrap-table-dictionary-resolve-failed",
                    "table": table_name,
                    "message": str(exc),
                }
            )
            continue
        owner_name = resolution.table.owner
        user_by_owner.setdefault(
            owner_name,
            {
                "dict_type": "user",
                "owner": owner_name,
                "source": "heuristic-system-scan",
                "schema_id": None,
                "status": "schema-id-not-decoded",
            },
        )
        table_rows.append(_table_dict_row(resolution))
        column_rows.extend(_column_dict_rows(resolution))
    return list(user_by_owner.values()), table_rows, column_rows, diagnostics


def _table_dict_row(resolution: Any) -> dict[str, Any]:
    return {
        "dict_type": "table",
        "owner": resolution.table.owner,
        "name": resolution.table.name,
        "qualified_name": resolution.table.qualified_name,
        "object_id": resolution.table_object_id,
        "storage_index_id": resolution.index_child.index_id,
        "group_id": resolution.table.storage.group_id,
        "root_file": resolution.table.storage.file_no,
        "root_page": resolution.table.storage.root_page,
        "scan_pages": resolution.table.storage.scan_pages,
        "source": "heuristic-system-scan",
        "sysobjects": {
            "offset": resolution.table_object.offset,
            "page_no": resolution.table_object.page_no,
            "page_offset": resolution.table_object.page_offset,
            "score": resolution.table_object.score,
        },
        "sysobject_index_child": {
            "name": resolution.index_child.name,
            "offset": resolution.index_child.offset,
            "page_no": resolution.index_child.page_no,
            "page_offset": resolution.index_child.page_offset,
            "score": resolution.index_child.score,
        },
        "sysindexes": {
            "offset": resolution.storage_index.offset,
            "page_no": resolution.storage_index.page_no,
            "page_offset": resolution.storage_index.page_offset,
            "score": resolution.storage_index.score,
            "type_name": resolution.storage_index.type_name,
            "flag": resolution.storage_index.flag,
        },
    }


def _column_dict_rows(resolution: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal, column in enumerate(resolution.columns, start=1):
        rows.append(
            {
                "dict_type": "column",
                "owner": resolution.table.owner,
                "table_name": resolution.table.name,
                "qualified_table_name": resolution.table.qualified_name,
                "object_id": resolution.table_object_id,
                "column_id": column.column_id,
                "ordinal": ordinal,
                "name": column.name,
                "type_name": column.type_name,
                "length": column.length,
                "source": "heuristic-system-scan",
                "syscolumns": {
                    "offset": column.offset,
                    "page_no": column.page_no,
                    "page_offset": column.page_offset,
                    "score": column.score,
                },
            }
        )
    return rows


def _dictionary_dump_status(
    *,
    requested_tables: tuple[str, ...],
    table_rows: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> str:
    if not requested_tables:
        return "not-requested"
    if diagnostics:
        return "partial" if table_rows else "failed"
    return "heuristic-output"


def _matched_control_entries(
    summary: dict[str, Any],
    file_entry: dict[str, Any],
) -> list[dict[str, Any]]:
    target_path = file_entry.get("path")
    manifest = summary.get("control_file_data_files")
    if not isinstance(manifest, dict):
        return []
    matches: list[dict[str, Any]] = []
    for entry in manifest.get("entries", ()):
        if not isinstance(entry, dict):
            continue
        matched_paths = entry.get("matched_paths")
        if not isinstance(matched_paths, list) or target_path not in matched_paths:
            continue
        matches.append(
            {
                "control_file_ordinal": entry.get("control_file_ordinal"),
                "source_control_file": entry.get("source_control_file"),
                "offset": entry.get("offset"),
                "text": entry.get("text"),
                "normalized_path": entry.get("normalized_path"),
                "basename": entry.get("basename"),
            }
        )
    return matches


def _bootstrap_diagnostics(
    summary: dict[str, Any],
    file_rows: list[dict[str, Any]],
    *,
    requested_tables: tuple[str, ...],
    table_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not file_rows:
        diagnostics.append(
            {
                "level": "error",
                "code": "bootstrap-no-data-files",
                "message": "no DBF files were available for file.dict",
            }
        )
    if requested_tables:
        diagnostics.append(
            {
                "level": "warning",
                "code": "bootstrap-sys-dictionaries-heuristic",
                "message": "user.dict, tab.dict, and col.dict were built from current SYSTEM.DBF heuristics; complete SYS row layouts are still not decoded",
            }
        )
        diagnostics.extend(table_diagnostics)
    else:
        diagnostics.append(
            {
                "level": "warning",
                "code": "bootstrap-sys-dictionaries-not-requested",
                "message": "user.dict, tab.dict, and col.dict are empty because no target table was requested",
            }
        )
    summary_diagnostics = summary.get("diagnostics")
    if isinstance(summary_diagnostics, dict):
        counts = summary_diagnostics.get("counts_by_code")
        if isinstance(counts, dict):
            for code, count in sorted(counts.items()):
                diagnostics.append(
                    {
                        "level": "info",
                        "code": f"database-summary-{code}",
                        "count": count,
                    }
                )
    return diagnostics


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")
