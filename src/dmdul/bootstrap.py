from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Callable

from .control_map import write_control_ctl
from .database_summary import summarize_database_dir
from .discovery import discover_data_files
from .resolver import OfflineResolveError, resolve_offline_table_metadata
from .sysdict import (
    SysObjectRowCandidate,
    dump_syscolumn_rows,
    dump_sysindex_rows,
    dump_sysobject_rows,
    find_sysindex_candidates,
)


DICT_FILENAMES = ("file.dict", "user.dict", "tab.dict", "col.dict")

DICT_FIELDNAMES = {
    "file.dict": (
        "dict_type",
        "ordinal",
        "path",
        "basename",
        "bytes",
        "page_size",
        "pages",
        "group_id",
        "file_no",
        "page_type_raw",
        "page0_kind_raw",
        "page0_kind_label",
        "system_candidate",
    ),
    "user.dict": (
        "dict_type",
        "owner",
        "schema_id",
        "source",
        "status",
    ),
    "tab.dict": (
        "dict_type",
        "object_kind",
        "owner",
        "name",
        "qualified_name",
        "object_id",
        "parent_object_id",
        "schema_id",
        "subtype_name",
        "storage_index_id",
        "group_id",
        "root_file",
        "root_page",
        "scan_pages",
        "source",
    ),
    "col.dict": (
        "dict_type",
        "owner",
        "table_name",
        "qualified_table_name",
        "object_id",
        "column_id",
        "ordinal",
        "name",
        "type_name",
        "length",
        "source",
    ),
}


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
    experimental_heuristic_dicts: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Write first-stage bootstrap dictionary artifacts.

    The current implementation can build `file.dict` from `dm.ctl` evidence and
    DBF page-0 headers. User/table/column dictionaries are created as explicit
    empty artifacts until SYSTEM dictionary rows are decoded without heuristics.
    """

    _emit_progress(progress, f"scan database directory: {database_dir}")
    summary = summarize_database_dir(
        database_dir=database_dir,
        page_size=page_size,
        catalog_pages=catalog_pages,
        sample_limit=sample_limit,
    )
    _emit_progress(
        progress,
        "database scan complete: "
        f"data_files={summary.get('files_total')} "
        f"control_files={summary.get('control_files_total')}",
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    dict_paths = {name: output_dir / name for name in DICT_FILENAMES}
    control_ctl_path = output_dir / "control.ctl"
    _emit_progress(progress, f"write control.ctl: {control_ctl_path}")
    control_ctl_manifest = write_control_ctl(
        database_dir=database_dir,
        output=control_ctl_path,
        page_size=page_size,
        sample_limit=sample_limit,
    )
    _emit_progress(progress, f"control.ctl rows={control_ctl_manifest['rows_total']}")
    file_rows = _file_dict_rows(summary)
    _emit_progress(progress, f"file.dict rows={len(file_rows)}")
    if experimental_heuristic_dicts and tables:
        _emit_progress(progress, f"resolve requested table dictionaries: tables={len(tables)}")
        user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_for_tables(
            database_dir=database_dir,
            tables=tables,
            owner=owner,
            page_size=page_size,
            scan_pages=scan_pages,
        )
    elif experimental_heuristic_dicts:
        _emit_progress(progress, "scan SYSTEM.DBF for SYSOBJECTS/SYSCOLUMNS/SYSINDEXES")
        user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_from_system_scan(
            database_dir=database_dir,
            owner=owner,
            page_size=page_size,
            scan_pages=scan_pages,
        )
    else:
        _emit_progress(progress, "dictionary download not requested; writing empty user/tab/col dicts")
        user_rows = []
        table_rows = []
        column_rows = []
        table_diagnostics = _heuristic_dicts_disabled_diagnostics(tables)
    _emit_progress(
        progress,
        "dictionary rows: "
        f"user={len(user_rows)} tab={len(table_rows)} col={len(column_rows)}",
    )
    _emit_progress(progress, f"write dictionary files: {output_dir}")
    _write_csv_dict(dict_paths["file.dict"], file_rows, DICT_FIELDNAMES["file.dict"])
    _write_csv_dict(dict_paths["user.dict"], user_rows, DICT_FIELDNAMES["user.dict"])
    _write_csv_dict(dict_paths["tab.dict"], table_rows, DICT_FIELDNAMES["tab.dict"])
    _write_csv_dict(dict_paths["col.dict"], column_rows, DICT_FIELDNAMES["col.dict"])

    manifest = {
        "mode": "dm-bootstrap-dicts",
        "database_dir": str(database_dir),
        "page_size": page_size,
        "control_ctl": str(control_ctl_path),
        "dict_files": {name: str(path) for name, path in dict_paths.items()},
        "rows": {
            "control.ctl": control_ctl_manifest["rows_total"],
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
                "output": ["control.ctl", "file.dict"],
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
                    experimental_heuristic_dicts=experimental_heuristic_dicts,
                ),
                "output": ["user.dict", "tab.dict", "col.dict"],
            },
        ],
        "requested_tables": list(tables),
        "experimental_heuristic_dicts": experimental_heuristic_dicts,
        "diagnostics": _bootstrap_diagnostics(
            summary,
            file_rows,
            control_ctl_manifest=control_ctl_manifest,
            requested_tables=tables,
            experimental_heuristic_dicts=experimental_heuristic_dicts,
            table_diagnostics=table_diagnostics,
        ),
        "database_summary": summary,
    }
    manifest_path = output_dir / "bootstrap_manifest.json"
    _emit_progress(progress, f"write bootstrap manifest: {manifest_path}")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    _emit_progress(progress, "bootstrap complete")
    return manifest


def _emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


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



def _dictionary_rows_from_system_scan(
    *,
    database_dir: Path,
    owner: str | None,
    page_size: int,
    scan_pages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    system_file = _find_system_file(database_dir, page_size=page_size)
    if system_file is None:
        return [], [], [], [
            {
                "level": "error",
                "code": "bootstrap-system-file-not-found",
                "message": "no SYSTEM.DBF candidate was found for SYSOBJECTS scan",
            }
        ]

    sysobjects = dump_sysobject_rows(system_file, page_size=page_size)
    table_objects = [
        row
        for row in sysobjects
        if row.type_name == "SCHOBJ"
        and row.subtype_name in {"UTAB", "STAB"}
        and row.object_id is not None
    ]
    storage_children = [
        row
        for row in sysobjects
        if row.type_name == "TABOBJ"
        and row.subtype_name == "INDEX"
        and row.object_id is not None
        and row.parent_id is not None
    ]
    storage_by_parent: dict[int, SysObjectRowCandidate] = {}
    for row in storage_children:
        assert row.parent_id is not None
        existing = storage_by_parent.get(row.parent_id)
        if existing is None or row.score > existing.score:
            storage_by_parent[row.parent_id] = row

    schema_rows = [
        row
        for row in sysobjects
        if row.type_name == "SCH" and row.object_id is not None and row.name
    ]
    owner_by_schema_id = {row.object_id: row.name for row in schema_rows}
    user_rows_by_schema_id: dict[int | str, dict[str, Any]] = {}
    if owner:
        user_rows_by_schema_id["override"] = {
            "dict_type": "user",
            "owner": owner,
            "schema_id": "",
            "source": "heuristic-system-scan",
            "status": "owner-override",
        }
    for schema_row in schema_rows:
        assert schema_row.object_id is not None
        user_rows_by_schema_id.setdefault(
            schema_row.object_id,
            {
                "dict_type": "user",
                "owner": schema_row.name,
                "schema_id": schema_row.object_id,
                "source": "heuristic-system-scan",
                "status": "sysobjects-schema-row",
            },
        )

    all_columns = dump_syscolumn_rows(system_file, page_size=page_size)
    columns_by_object_id: dict[int, list[Any]] = {}
    for column in all_columns:
        columns_by_object_id.setdefault(column.object_id, []).append(column)

    all_indexes = dump_sysindex_rows(system_file, page_size=page_size)
    sysindex_by_id = {index.index_id: index for index in all_indexes}

    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    for table in table_objects:
        assert table.object_id is not None
        storage_child = storage_by_parent.get(table.object_id)
        storage_index = None
        if storage_child is not None and storage_child.object_id is not None:
            storage_index = sysindex_by_id.get(storage_child.object_id)
        owner_name = owner or (owner_by_schema_id.get(table.schema_id) if table.schema_id is not None else "") or ""
        if table.schema_id is not None and owner_name and table.schema_id not in user_rows_by_schema_id:
            user_rows_by_schema_id[table.schema_id] = {
                "dict_type": "user",
                "owner": owner_name,
                "schema_id": table.schema_id,
                "source": "heuristic-system-scan",
                "status": "schema-id-owner-resolved",
            }
        qualified_name = f"{owner_name}.{table.name}" if owner_name else table.name
        table_rows.append(
            {
                "dict_type": "table",
                "object_kind": "table",
                "owner": owner_name,
                "name": table.name,
                "qualified_name": qualified_name,
                "object_id": table.object_id,
                "schema_id": table.schema_id,
                "subtype_name": table.subtype_name,
                "storage_index_id": None if storage_child is None else storage_child.object_id,
                "group_id": None if storage_index is None else storage_index.group_id,
                "root_file": None if storage_index is None else storage_index.root_file,
                "root_page": None if storage_index is None else storage_index.root_page,
                "scan_pages": scan_pages,
                "source": "heuristic-system-scan",
                "sysobjects": {
                    "offset": table.offset,
                    "page_no": table.page_no,
                    "page_offset": table.page_offset,
                    "score": table.score,
                },
                "sysobject_index_child": None if storage_child is None else {
                    "name": storage_child.name,
                    "offset": storage_child.offset,
                    "page_no": storage_child.page_no,
                    "page_offset": storage_child.page_offset,
                    "score": storage_child.score,
                },
                "sysindexes": None if storage_index is None else {
                    "offset": storage_index.offset,
                    "page_no": storage_index.page_no,
                    "page_offset": storage_index.page_offset,
                    "score": storage_index.score,
                    "type_name": storage_index.type_name,
                    "flag": storage_index.flag,
                },
            }
        )
        column_rows.extend(
            _column_dict_rows_from_system_scan(
                columns=columns_by_object_id.get(table.object_id, []),
                owner_name=owner_name,
                table=table,
            )
        )

    table_rows.extend(
        _index_dict_rows_from_system_scan(
            owner_by_schema_id=owner_by_schema_id,
            owner_override=owner,
            storage_children=storage_children,
            sysindex_by_id=sysindex_by_id,
        )
    )

    diagnostics.append(
        {
            "level": "warning",
            "code": "bootstrap-system-dictionary-scan-heuristic",
            "message": "SYSOBJECTS/SYSCOLUMNS/SYSINDEXES were scanned directly from SYSTEM.DBF with current calibrated heuristics; schema/user rows are not yet fully decoded",
            "system_file": str(system_file),
            "sysobjects_rows": len(sysobjects),
            "table_rows": len(table_objects),
            "index_rows": len(storage_children),
            "tab_dict_rows": len(table_rows),
            "storage_child_rows": len(storage_children),
            "syscolumns_rows": len(all_columns),
            "sysindexes_rows": len(all_indexes),
            "column_rows": len(column_rows),
            "schema_rows": len(schema_rows),
            "schema_owner_rows": len(user_rows_by_schema_id),
        }
    )
    return list(user_rows_by_schema_id.values()), table_rows, column_rows, diagnostics



def _index_dict_rows_from_system_scan(
    *,
    owner_by_schema_id: dict[int, str],
    owner_override: str | None,
    storage_children: list[SysObjectRowCandidate],
    sysindex_by_id: dict[int, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for child in storage_children:
        if child.object_id is None:
            continue
        storage_index = sysindex_by_id.get(child.object_id)
        owner_name = owner_override or (owner_by_schema_id.get(child.schema_id) if child.schema_id is not None else "") or ""
        rows.append(
            {
                "dict_type": "table",
                "object_kind": "index",
                "owner": owner_name,
                "name": child.name,
                "qualified_name": f"{owner_name}.{child.name}" if owner_name else child.name,
                "object_id": child.object_id,
                "parent_object_id": child.parent_id,
                "schema_id": child.schema_id,
                "subtype_name": child.subtype_name,
                "storage_index_id": child.object_id,
                "group_id": None if storage_index is None else storage_index.group_id,
                "root_file": None if storage_index is None else storage_index.root_file,
                "root_page": None if storage_index is None else storage_index.root_page,
                "scan_pages": 1,
                "source": "heuristic-system-scan",
                "sysobjects": {
                    "offset": child.offset,
                    "page_no": child.page_no,
                    "page_offset": child.page_offset,
                    "score": child.score,
                },
                "sysindexes": None if storage_index is None else {
                    "offset": storage_index.offset,
                    "page_no": storage_index.page_no,
                    "page_offset": storage_index.page_offset,
                    "score": storage_index.score,
                    "type_name": storage_index.type_name,
                    "flag": storage_index.flag,
                },
            }
        )
    return rows


def _find_system_file(database_dir: Path, *, page_size: int) -> Path | None:
    files = discover_data_files(database_dir, page_size=page_size)
    for item in files:
        if item.is_system_candidate:
            return item.path
    for item in files:
        if item.path.name.upper() == "SYSTEM.DBF":
            return item.path
    return None


def _column_dict_rows_from_system_scan(
    *,
    columns: list[Any],
    owner_name: str,
    table: SysObjectRowCandidate,
) -> list[dict[str, Any]]:
    if table.object_id is None:
        return []
    rows: list[dict[str, Any]] = []
    for ordinal, column in enumerate(columns, start=1):
        rows.append(
            {
                "dict_type": "column",
                "owner": owner_name,
                "table_name": table.name,
                "qualified_table_name": f"{owner_name}.{table.name}" if owner_name else table.name,
                "object_id": table.object_id,
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


def _heuristic_dicts_disabled_diagnostics(
    tables: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not tables:
        return []
    return [
        {
            "level": "warning",
            "code": "bootstrap-heuristic-dictionary-output-disabled",
            "tables": list(tables),
            "message": (
                "target table names were supplied, but user.dict/tab.dict/col.dict "
                "remain empty unless experimental heuristic dictionary output is "
                "explicitly enabled; full dictionary bootstrap requires proven "
                "row and data-type storage decoding first"
            ),
        }
    ]


def _table_dict_row(resolution: Any) -> dict[str, Any]:
    return {
        "dict_type": "table",
        "object_kind": "table",
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
    experimental_heuristic_dicts: bool,
) -> str:
    if not experimental_heuristic_dicts:
        return "blocked-by-type-decoding" if requested_tables else "not-requested"
    if not requested_tables and table_rows:
        return "system-scan-output"
    if not requested_tables:
        return "failed" if diagnostics else "not-requested"
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
    control_ctl_manifest: dict[str, Any],
    requested_tables: tuple[str, ...],
    experimental_heuristic_dicts: bool,
    table_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for item in control_ctl_manifest.get("diagnostics", ()):
        if isinstance(item, dict):
            diagnostics.append(item)
    if not file_rows:
        diagnostics.append(
            {
                "level": "error",
                "code": "bootstrap-no-data-files",
                "message": "no DBF files were available for file.dict",
            }
        )
    if experimental_heuristic_dicts:
        diagnostics.append(
            {
                "level": "warning",
                "code": "bootstrap-sys-dictionaries-heuristic",
                "message": "user.dict, tab.dict, and col.dict were built from current SYSTEM.DBF heuristics; complete SYS row layouts are still not decoded",
            }
        )
        diagnostics.extend(table_diagnostics)
    elif requested_tables:
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


def _write_csv_dict(
    path: Path,
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    fieldnames: tuple[str, ...],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_scalar(row.get(key)) for key in fieldnames})


def _csv_scalar(value: Any) -> str | int | bool:
    if value is None:
        return ""
    if isinstance(value, (str, int, bool)):
        return value
    return json.dumps(value, sort_keys=True)
