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
    dump_syscolumn_rows_from_storage,
    dump_sysindex_rows,
    dump_sysindex_rows_from_storage,
    dump_sysobject_rows,
    dump_sysobject_rows_from_storage,
    discover_system_dictionary_root_from_file_header,
    discover_storage_root_page,
    find_sysindex_candidates,
)


DICT_FILENAMES = ("file.dict", "user.dict", "tab.dict", "col.dict")

SYSTEM_DICTIONARY_STORAGE_IDS = {
    # Verified DM8 bootstrap roots in SYSTEM.DBF. These are used only for
    # fixed system dictionary tables when SYSOBJECTS child-object heuristics
    # produce an unverified storage child. The root still has to be confirmed
    # through SYSINDEXES and the page header storage id.
    0: 33554540,  # SYS.SYSOBJECTS
    1: 33554434,  # SYS.SYSINDEXES
    2: 33554433,  # SYS.SYSCOLUMNS
}

SYSTEM_DICTIONARY_STORAGE_ROOTS = {
    0: {"name": "SYSOBJECTS", "storage_id": 33554540, "group_id": 0, "root_file": 0, "root_page": 16},
    1: {"name": "SYSINDEXES", "storage_id": 33554434, "group_id": 0, "root_file": 0, "root_page": 288},
    2: {"name": "SYSCOLUMNS", "storage_id": 33554433, "group_id": 0, "root_file": 0, "root_page": 80},
}

DM_BUILTIN_SCHEMA_NAMES_BY_ID = {
    0x09000000: "SYS",
    0x09000001: "SYSDBA",
    0x09000002: "SYSAUDITOR",
    0x09000003: "SYSSSO",
    0x09000004: "CTISYS",
}


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
        "storage_index_ids",
        "group_id",
        "root_file",
        "root_page",
        "page_refs",
        "partition_names",
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
        "scale",
        "nullable",
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
    source_dict_dir: Path | None = None,
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
    if experimental_heuristic_dicts and tables and source_dict_dir is not None:
        _emit_progress(progress, f"filter requested table dictionaries from source_dict_dir={source_dict_dir}")
        user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_from_existing_dicts(
            source_dict_dir=source_dict_dir,
            tables=tables,
            owner=owner,
        )
    elif experimental_heuristic_dicts and tables:
        _emit_progress(progress, f"resolve requested table dictionaries: tables={len(tables)}")
        user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_for_tables(
            database_dir=database_dir,
            tables=tables,
            owner=owner,
            page_size=page_size,
            scan_pages=scan_pages,
            progress=progress,
        )
    elif experimental_heuristic_dicts:
        _emit_progress(progress, "download SYS dictionaries from SYSTEM storage roots")
        user_rows, table_rows, column_rows, table_diagnostics = _dictionary_rows_from_system_scan(
            database_dir=database_dir,
            owner=owner,
            page_size=page_size,
            scan_pages=scan_pages,
            progress=progress,
            output_dir=output_dir,
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
        "source_dict_dir": None if source_dict_dir is None else str(source_dict_dir),
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
    progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    user_by_owner: dict[str, dict[str, Any]] = {}
    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    system_file = _find_system_file(database_dir, page_size=page_size)
    sysobject_rows: tuple[SysObjectRowCandidate, ...] | None = None
    if system_file is None:
        diagnostics.append(
            {
                "level": "error",
                "code": "bootstrap-system-file-not-found",
                "message": "no SYSTEM.DBF candidate was found for requested table resolution",
            }
        )
    else:
        _emit_progress(progress, f"SYSOBJECTS preload start: file={system_file}")
        sysobject_rows = tuple(_dump_sysobjects_for_bootstrap(system_file, page_size=page_size, progress=progress))
        _emit_progress(progress, f"SYSOBJECTS preload rows={len(sysobject_rows)}")
    for table_name in tables:
        try:
            resolution = resolve_offline_table_metadata(
                database_dir=database_dir,
                table_name=table_name,
                page_size=page_size,
                owner=owner,
                scan_pages=scan_pages,
                sysobject_rows=sysobject_rows,
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
                "schema_id": resolution.schema_id,
                "status": "schema-id-decoded" if resolution.schema_id is not None else "schema-id-not-decoded",
            },
        )
        table_rows.append(_table_dict_row(resolution))
        column_rows.extend(_column_dict_rows(resolution))
    return list(user_by_owner.values()), table_rows, column_rows, diagnostics


def _dictionary_rows_from_existing_dicts(
    *,
    source_dict_dir: Path,
    tables: tuple[str, ...],
    owner: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    try:
        source_user_rows = _read_csv_dict(source_dict_dir / "user.dict")
        source_table_rows = _read_csv_dict(source_dict_dir / "tab.dict")
        source_column_rows = _read_csv_dict(source_dict_dir / "col.dict")
    except FileNotFoundError as exc:
        return [], [], [], [
            {
                "level": "error",
                "code": "bootstrap-source-dict-missing",
                "message": str(exc),
                "source_dict_dir": str(source_dict_dir),
            }
        ]

    selected_tables: list[dict[str, Any]] = []
    for requested in tables:
        requested_owner, requested_name = _split_requested_table(requested, owner=owner)
        matches = [
            row
            for row in source_table_rows
            if (row.get("object_kind") or "table") == "table"
            and str(row.get("name") or "").upper() == requested_name
            and str(row.get("owner") or "").upper() == requested_owner
        ]
        if not matches:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "bootstrap-source-dict-table-not-found",
                    "table": requested,
                    "owner": requested_owner,
                    "source_dict_dir": str(source_dict_dir),
                }
            )
            continue
        selected_tables.append(matches[0])

    selected_object_ids = {str(row.get("object_id") or "") for row in selected_tables}
    selected_qualified_names = {str(row.get("qualified_name") or "").upper() for row in selected_tables}
    selected_columns = [
        row
        for row in source_column_rows
        if str(row.get("object_id") or "") in selected_object_ids
        and str(row.get("qualified_table_name") or "").upper() in selected_qualified_names
    ]
    selected_owners = {str(row.get("owner") or "") for row in selected_tables}
    source_users_by_owner = {str(row.get("owner") or ""): row for row in source_user_rows}
    selected_users: list[dict[str, Any]] = []
    for selected_owner in sorted(selected_owners):
        source_user = source_users_by_owner.get(selected_owner)
        if source_user is not None:
            selected_users.append(source_user)
            continue
        selected_table = next(row for row in selected_tables if str(row.get("owner") or "") == selected_owner)
        selected_users.append(
            {
                "dict_type": "user",
                "owner": selected_owner,
                "schema_id": selected_table.get("schema_id"),
                "source": "source-dict-filter",
                "status": "schema-id-from-table-row",
            }
        )
    return selected_users, selected_tables, selected_columns, diagnostics


def _split_requested_table(table_name: str, *, owner: str | None) -> tuple[str, str]:
    if "." in table_name:
        requested_owner, requested_name = table_name.split(".", 1)
        return requested_owner.upper(), requested_name.upper()
    return (owner or "SYSDBA").upper(), table_name.upper()


def _dump_sysobjects_for_bootstrap(
    system_file: Path,
    *,
    page_size: int,
    progress: Callable[[str], None] | None,
) -> list[SysObjectRowCandidate]:
    entry = _system_dictionary_storage_entry(
        system_file,
        object_id=0,
        page_size=page_size,
        progress=progress,
    )
    try:
        rows = dump_sysobject_rows_from_storage(
            system_file,
            group_id=int(entry["group_id"]),
            root_file=int(entry["root_file"]),
            root_page=int(entry["root_page"]),
            storage_id=int(entry["storage_id"]),
            page_size=page_size,
            progress=progress,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback path
        _emit_progress(progress, f"SYSOBJECTS storage download failed; fallback full scan: {exc}")
        return dump_sysobject_rows(system_file, page_size=page_size, progress=progress)
    if rows:
        return rows
    _emit_progress(progress, "SYSOBJECTS storage download returned 0 rows; fallback full scan")
    return dump_sysobject_rows(system_file, page_size=page_size, progress=progress)


def _dump_sysindexes_for_bootstrap(
    system_file: Path,
    *,
    page_size: int,
    progress: Callable[[str], None] | None,
) -> list[Any]:
    entry = _system_dictionary_storage_entry(
        system_file,
        object_id=1,
        page_size=page_size,
        progress=progress,
    )
    try:
        rows = dump_sysindex_rows_from_storage(
            system_file,
            group_id=int(entry["group_id"]),
            root_file=int(entry["root_file"]),
            root_page=int(entry["root_page"]),
            storage_id=int(entry["storage_id"]),
            page_size=page_size,
            progress=progress,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback path
        _emit_progress(progress, f"SYSINDEXES storage download failed; fallback full scan: {exc}")
        return dump_sysindex_rows(system_file, page_size=page_size, progress=progress)
    if rows:
        return rows
    _emit_progress(progress, "SYSINDEXES storage download returned 0 rows; fallback full scan")
    return dump_sysindex_rows(system_file, page_size=page_size, progress=progress)


def _system_dictionary_storage_entry(
    system_file: Path,
    *,
    object_id: int,
    page_size: int,
    progress: Callable[[str], None] | None,
) -> dict[str, int | str]:
    fixed = SYSTEM_DICTIONARY_STORAGE_ROOTS[object_id]
    root_file = int(fixed["root_file"])
    header_entry = discover_system_dictionary_root_from_file_header(
        system_file,
        object_id=object_id,
        root_file=root_file,
        page_size=page_size,
    )
    if header_entry is not None:
        _emit_progress(
            progress,
            f"{fixed['name']} root discovered from SYSTEM file header: "
            f"storage_id={header_entry.storage_id} root_file={header_entry.root_file} "
            f"root_page={header_entry.root_page} source={header_entry.source}",
        )
        return {
            "name": fixed["name"],
            "storage_id": header_entry.storage_id,
            "group_id": int(fixed["group_id"]),
            "root_file": header_entry.root_file,
            "root_page": header_entry.root_page,
        }
    storage_id = int(fixed["storage_id"])
    discovered = discover_storage_root_page(
        system_file,
        storage_id=storage_id,
        root_file=root_file,
        page_size=page_size,
    )
    if discovered is None:
        _emit_progress(
            progress,
            f"{fixed['name']} root discovery failed; fallback fixed root_page={fixed['root_page']}",
        )
        return fixed
    _emit_progress(
        progress,
        f"{fixed['name']} root discovered: "
        f"storage_id={storage_id} root_file={discovered.root_file} "
        f"root_page={discovered.root_page} source={discovered.source}",
    )
    return {
        "name": fixed["name"],
        "storage_id": storage_id,
        "group_id": int(fixed["group_id"]),
        "root_file": discovered.root_file,
        "root_page": discovered.root_page,
    }



def _dictionary_rows_from_system_scan(
    *,
    database_dir: Path,
    owner: str | None,
    page_size: int,
    scan_pages: int,
    progress: Callable[[str], None] | None = None,
    output_dir: Path | None = None,
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

    _emit_progress(progress, f"SYSOBJECTS download start: file={system_file}")
    sysobjects = _dump_sysobjects_for_bootstrap(system_file, page_size=page_size, progress=progress)
    _emit_progress(progress, f"SYSOBJECTS rows={len(sysobjects)}")
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
    owner_by_schema_id = dict(DM_BUILTIN_SCHEMA_NAMES_BY_ID)
    owner_by_schema_id.update(
        {
            row.object_id: row.name
            for row in schema_rows
            if row.object_id is not None and 0x09000000 <= row.object_id <= 0x09FFFFFF
        }
    )
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

    _emit_progress(progress, f"SYSINDEXES download start: file={system_file}")
    all_indexes = _dump_sysindexes_for_bootstrap(system_file, page_size=page_size, progress=progress)
    _emit_progress(progress, f"SYSINDEXES rows={len(all_indexes)}")
    sysindex_by_id = {index.index_id: index for index in all_indexes}

    syscolumns_storage_id = SYSTEM_DICTIONARY_STORAGE_IDS.get(2)
    syscolumns_storage_index = (
        sysindex_by_id.get(syscolumns_storage_id)
        if syscolumns_storage_id is not None
        else None
    )
    if syscolumns_storage_index is None:
        syscolumns_storage_child = storage_by_parent.get(2)
        syscolumns_storage_id = (
            syscolumns_storage_child.object_id
            if syscolumns_storage_child is not None and syscolumns_storage_child.object_id is not None
            else None
        )
        syscolumns_storage_index = (
            sysindex_by_id.get(syscolumns_storage_id)
            if syscolumns_storage_id is not None
            else None
        )
    if (
        syscolumns_storage_id is not None
        and syscolumns_storage_index is not None
        and syscolumns_storage_index.root_file is not None
        and syscolumns_storage_index.root_page is not None
    ):
        failure_path = None if output_dir is None else output_dir / "syscolumns_failed.dict"
        _emit_progress(
            progress,
            "SYSCOLUMNS storage download start: "
            f"storage_id={syscolumns_storage_id} "
            f"root_file={syscolumns_storage_index.root_file} "
            f"root_page={syscolumns_storage_index.root_page} "
            f"failure_file={failure_path}",
        )
        all_columns = dump_syscolumn_rows_from_storage(
            system_file,
            group_id=0 if syscolumns_storage_index.group_id is None else syscolumns_storage_index.group_id,
            root_file=syscolumns_storage_index.root_file,
            root_page=syscolumns_storage_index.root_page,
            storage_id=syscolumns_storage_id,
            page_size=page_size,
            failure_path=failure_path,
            progress=progress,
        )
        _emit_progress(progress, f"SYSCOLUMNS storage rows={len(all_columns)}")
    else:
        _emit_progress(progress, "SYSCOLUMNS storage root not found; fallback raw scan start")
        all_columns = dump_syscolumn_rows(system_file, page_size=page_size, progress=progress)
        _emit_progress(progress, f"SYSCOLUMNS fallback scan rows={len(all_columns)}")
    columns_by_object_id: dict[int, list[Any]] = {}
    for column in all_columns:
        columns_by_object_id.setdefault(column.object_id, []).append(column)

    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    emitted_column_object_ids: set[int] = set()
    for table in table_objects:
        assert table.object_id is not None
        leaf_partitions = _leaf_partition_objects_for_table(
            table_objects=table_objects,
            table=table,
        )
        storage_children_for_table = [
            storage_by_parent.get(partition.object_id)
            for partition in leaf_partitions
            if partition.object_id is not None
        ] if leaf_partitions else [storage_by_parent.get(table.object_id)]
        storage_children_for_table = [
            child for child in storage_children_for_table if child is not None and child.object_id is not None
        ]
        storage_child = storage_children_for_table[0] if storage_children_for_table else None
        storage_indexes_for_table = [
            sysindex_by_id.get(child.object_id)
            for child in storage_children_for_table
            if child.object_id is not None
        ]
        storage_indexes_for_table = [item for item in storage_indexes_for_table if item is not None]
        storage_index = storage_indexes_for_table[0] if storage_indexes_for_table else None
        page_refs = _page_refs_from_storage_indexes(storage_indexes_for_table)
        partition_names = _join_texts(partition.name for partition in leaf_partitions)
        owner_name = owner or _owner_for_schema_id(table.schema_id, owner_by_schema_id) or ""
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
                "storage_index_ids": _join_ints(child.object_id for child in storage_children_for_table),
                "group_id": None if storage_index is None else storage_index.group_id,
                "root_file": None if storage_index is None else storage_index.root_file,
                "root_page": None if storage_index is None else storage_index.root_page,
                "page_refs": page_refs,
                "partition_names": partition_names,
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
        table_columns = columns_by_object_id.get(table.object_id, [])
        if table_columns and table.object_id not in emitted_column_object_ids:
            emitted_column_object_ids.add(table.object_id)
            column_rows.extend(
                _column_dict_rows_from_system_scan(
                    columns=table_columns,
                    owner_name=owner_name,
                    table=table,
                )
            )

    column_rows.extend(
        _unmatched_column_dict_rows_from_system_scan(
            columns_by_object_id=columns_by_object_id,
            matched_object_ids=emitted_column_object_ids,
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
            "message": "SYSOBJECTS/SYSINDEXES/SYSCOLUMNS were downloaded from SYSTEM.DBF storage roots with current calibrated heuristics; schema/user rows are not yet fully decoded",
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
        owner_name = owner_override or _owner_for_schema_id(child.schema_id, owner_by_schema_id) or ""
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


def _leaf_partition_objects_for_table(
    *,
    table_objects: list[SysObjectRowCandidate],
    table: SysObjectRowCandidate,
) -> list[SysObjectRowCandidate]:
    if table.object_id is None:
        return []
    children_by_parent: dict[int, list[SysObjectRowCandidate]] = {}
    for row in table_objects:
        if row.object_id is None or row.parent_id is None:
            continue
        children_by_parent.setdefault(row.parent_id, []).append(row)
    leaves: list[SysObjectRowCandidate] = []

    def visit(object_id: int) -> None:
        children = children_by_parent.get(object_id, [])
        for child in children:
            assert child.object_id is not None
            if child.object_id in children_by_parent:
                visit(child.object_id)
            else:
                leaves.append(child)

    visit(table.object_id)
    return sorted(leaves, key=lambda item: (item.object_id or 0, item.name))


def _page_refs_from_storage_indexes(storage_indexes: list[Any]) -> str:
    refs: list[str] = []
    for storage_index in storage_indexes:
        if storage_index.root_file is None or storage_index.root_page is None:
            continue
        refs.append(f"{storage_index.root_file}:{storage_index.root_page}")
    return ";".join(refs)


def _join_ints(values: Any) -> str:
    return ";".join(str(value) for value in values if value is not None)


def _join_texts(values: Any) -> str:
    return ";".join(str(value) for value in values if value not in {None, ""})


def _find_system_file(database_dir: Path, *, page_size: int) -> Path | None:
    files = discover_data_files(database_dir, page_size=page_size)
    for item in files:
        if item.is_system_candidate:
            return item.path
    for item in files:
        if item.path.name.upper() == "SYSTEM.DBF":
            return item.path
    return None


def _owner_for_schema_id(
    schema_id: int | None,
    owner_by_schema_id: dict[int, str],
) -> str | None:
    if schema_id is None:
        return None
    return owner_by_schema_id.get(schema_id)


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
                "scale": column.scale,
                "nullable": column.nullable,
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




def _unmatched_column_dict_rows_from_system_scan(
    *,
    columns_by_object_id: dict[int, list[Any]],
    matched_object_ids: set[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for object_id in sorted(columns_by_object_id):
        if object_id in matched_object_ids:
            continue
        columns = columns_by_object_id[object_id]
        for ordinal, column in enumerate(columns, start=1):
            rows.append(
                {
                    "dict_type": "column",
                    "owner": "",
                    "table_name": "",
                    "qualified_table_name": "",
                    "object_id": object_id,
                    "column_id": column.column_id,
                    "ordinal": ordinal,
                    "name": column.name,
                    "type_name": column.type_name,
                    "length": column.length,
                    "scale": column.scale,
                    "nullable": column.nullable,
                    "source": "syscolumns-storage-unmatched-table",
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
    page_refs = ";".join(
        f"{item.file_no}:{item.page_no}"
        for item in resolution.table.storage.page_refs
    )
    storage_index_ids = _join_ints(
        item.index_id
        for item in (resolution.partition_index_children or (resolution.index_child,))
    )
    partition_names = _join_texts(
        item.name
        for item in resolution.partition_objects
    )
    return {
        "dict_type": "table",
        "object_kind": "table",
        "owner": resolution.table.owner,
        "name": resolution.table.name,
        "qualified_name": resolution.table.qualified_name,
        "object_id": resolution.table_object_id,
        "schema_id": resolution.schema_id,
        "storage_index_id": resolution.index_child.index_id,
        "storage_index_ids": storage_index_ids,
        "group_id": resolution.table.storage.group_id,
        "root_file": resolution.table.storage.file_no,
        "root_page": resolution.table.storage.root_page,
        "page_refs": page_refs,
        "partition_names": partition_names,
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
                "scale": column.scale,
                "nullable": column.nullable,
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
        return "system-storage-output"
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


def _read_csv_dict(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _csv_scalar(value: Any) -> str | int | bool:
    if value is None:
        return ""
    if isinstance(value, (str, int, bool)):
        return value
    return json.dumps(value, sort_keys=True)
