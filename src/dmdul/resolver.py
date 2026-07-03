from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .discovery import DiscoveredDataFile, discover_data_files
from .database_summary import summarize_database_dir
from .metadata import (
    CalibratedMetadata,
    ColumnMeta,
    DataFileMeta,
    StoragePageRef,
    StorageRoot,
    TableMeta,
)
from .segment import analyze_segment_root
from .sysdict import (
    SysColumnCandidate,
    SysIndexCandidate,
    SysObjectCandidate,
    SysObjectIndexChildCandidate,
    SysObjectRowCandidate,
    dump_sysobject_rows,
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)


class OfflineResolveError(RuntimeError):
    pass


BUILTIN_SCHEMA_IDS_BY_OWNER = {
    "SYS": 0x09000000,
    "SYSDBA": 0x09000001,
    "SYSAUDITOR": 0x09000002,
    "SYSSSO": 0x09000003,
    "CTISYS": 0x09000004,
}


@dataclass(frozen=True)
class OfflineTableResolution:
    metadata: CalibratedMetadata
    table: TableMeta
    system_file: Path
    table_object: SysObjectCandidate
    table_object_id: int
    index_child: SysObjectIndexChildCandidate
    storage_index: SysIndexCandidate
    columns: tuple[SysColumnCandidate, ...]
    schema_id: int | None = None
    control_file_data_files: dict[str, object] | None = None
    segment_root: dict[str, object] | None = None
    partition_objects: tuple[SysObjectRowCandidate, ...] = ()
    partition_index_children: tuple[SysObjectIndexChildCandidate, ...] = ()

    def as_manifest(self) -> dict[str, object]:
        data_files = [
            {
                "group_id": item.group_id,
                "file_no": item.file_no,
                "path": str(item.path),
                "page_size": item.page_size,
                "control_file_entries": _control_file_entries_for_path(
                    control_file_data_files=self.control_file_data_files,
                    path=item.path,
                ),
            }
            for item in self.metadata.data_files
        ]
        return {
            "system_file": str(self.system_file),
            "table": self.table.qualified_name,
            "table_object_id": self.table_object_id,
            "storage_index_id": self.index_child.index_id,
            "diagnostics": _manifest_diagnostics(
                data_files=data_files,
                segment_root=self.segment_root,
            ),
            "storage": {
                "group_id": self.table.storage.group_id,
                "file_no": self.table.storage.file_no,
                "root_page": self.table.storage.root_page,
                "scan_pages": self.table.storage.scan_pages,
            },
            "table_object": {
                "object_id": self.table_object_id,
                "name": self.table_object.name,
                "offset": self.table_object.offset,
                "page_no": self.table_object.page_no,
                "page_offset": self.table_object.page_offset,
                "score": self.table_object.score,
            },
            "columns": [
                {
                    "name": column.name,
                    "type_name": column.type_name,
                    "length": column.length,
                    "scale": column.scale,
                    "nullable": column.nullable,
                    "column_id": column.column_id,
                    "offset": column.offset,
                    "page_no": column.page_no,
                    "page_offset": column.page_offset,
                    "score": column.score,
                }
                for column in self.columns
            ],
            "segment": {
                "storage_index_id": self.index_child.index_id,
                "index_child_name": self.index_child.name,
                "index_child_offset": self.index_child.offset,
                "index_child_page_no": self.index_child.page_no,
                "sysindexes_offset": self.storage_index.offset,
                "sysindexes_page_no": self.storage_index.page_no,
                "group_id": self.table.storage.group_id,
                "root_file": self.table.storage.file_no,
                "root_page": self.table.storage.root_page,
                "scan_pages": self.table.storage.scan_pages,
                "type_name": self.storage_index.type_name,
                "flag": self.storage_index.flag,
            },
            "partitions": [
                {
                    "name": row.name,
                    "object_id": row.object_id,
                    "parent_id": row.parent_id,
                    "schema_id": row.schema_id,
                    "offset": row.offset,
                    "page_no": row.page_no,
                    "page_offset": row.page_offset,
                }
                for row in self.partition_objects
            ],
            "partition_storage_indexes": [
                {
                    "parent_object_id": item.parent_object_id,
                    "storage_index_id": item.index_id,
                    "name": item.name,
                    "offset": item.offset,
                    "page_no": item.page_no,
                    "page_offset": item.page_offset,
                }
                for item in self.partition_index_children
            ],
            "data_files": data_files,
            "control_file_data_files": self.control_file_data_files,
            "segment_root": self.segment_root,
            "mode": "dmctl-system-sysdict-segment-root",
        }


def resolve_offline_table_metadata(
    *,
    database_dir: Path,
    table_name: str,
    page_size: int = 8192,
    owner: str | None = None,
    scan_pages: int = 64,
    sysobject_rows: tuple[SysObjectRowCandidate, ...] | None = None,
) -> OfflineTableResolution:
    """Resolve enough offline metadata to scan an ordinary table.

    This composes the currently calibrated SYS dictionary heuristics. It is
    intentionally narrow: object id, columns, child storage index, and storage
    root are recovered from SYSTEM.DBF, while data files are discovered from
    page-0 headers.
    """

    database_summary = summarize_database_dir(
        database_dir=database_dir,
        page_size=page_size,
        catalog_pages=0,
    )
    files = discover_data_files(database_dir, page_size=page_size)
    system_file = _select_system_file(files)
    owner_name, object_name = _split_table_name(table_name, owner=owner)
    resolved_sysobject_rows = tuple(
        sysobject_rows
        if sysobject_rows is not None
        else _cached_sysobject_rows_for_file(system_file.path, page_size=page_size)
    )
    table_object_row = _select_owner_table_object_row(
        system_file.path,
        table_name=object_name,
        owner_name=owner_name,
        page_size=page_size,
        sysobject_rows=resolved_sysobject_rows,
    )
    table_schema_id = None
    if table_object_row is not None:
        table_object = _table_object_candidate_from_row(table_object_row)
        table_object_id = _required_int(table_object_row.object_id, "table object id")
        table_schema_id = table_object_row.schema_id
    else:
        table_object = _select_table_object(
            find_sysobject_candidates(system_file.path, object_name, page_size=page_size),
            table_name=object_name,
        )
        table_object_id = _select_table_object_id(table_object)
    columns = _select_columns(
        find_syscolumn_candidates(system_file.path, table_object_id, page_size=page_size)
    )
    partition_objects = _select_leaf_partition_object_rows(
        resolved_sysobject_rows,
        parent_object_id=table_object_id,
        schema_id=table_schema_id,
    )
    if partition_objects:
        partition_index_children = tuple(
            _select_index_child(
                _index_child_candidates_from_sysobject_rows(
                    resolved_sysobject_rows,
                    parent_object_id=_required_int(partition.object_id, "partition object id"),
                )
            )
            for partition in partition_objects
        )
        partition_storage_indexes = tuple(
            _select_storage_index(
                find_sysindex_candidates(
                    system_file.path,
                    index_child.index_id,
                    page_size=page_size,
                )
            )
            for index_child in partition_index_children
        )
        index_child = partition_index_children[0]
        storage_index = partition_storage_indexes[0]
    else:
        partition_index_children = ()
        partition_storage_indexes = ()
        index_child = _select_index_child(
            _index_child_candidates_from_sysobject_rows(
                resolved_sysobject_rows,
                parent_object_id=table_object_id,
            )
            or find_sysobject_index_child_candidates(
                system_file.path,
                table_object_id,
                page_size=page_size,
            )
        )
        storage_index = _select_storage_index(
            find_sysindex_candidates(
                system_file.path,
                index_child.index_id,
                page_size=page_size,
            )
        )
    storage_indexes_for_plan = partition_storage_indexes or (storage_index,)
    data_file = _select_data_file(
        files,
        group_id=_required_int(storage_index.group_id, "storage group id"),
        file_no=_required_int(storage_index.root_file, "storage root file"),
    )
    segment_root = _analyze_storage_roots(
        files=files,
        storage_indexes=storage_indexes_for_plan,
        page_size=page_size,
    )
    page_refs = tuple(
        StoragePageRef(
            file_no=_required_int(item.root_file, "storage root file"),
            page_no=_required_int(item.root_page, "storage root page"),
        )
        for item in storage_indexes_for_plan
    )
    table = TableMeta(
        owner=owner_name,
        name=object_name,
        columns=tuple(
            ColumnMeta(
                name=column.name,
                type_name=column.type_name,
                length=column.length,
                scale=column.scale,
                nullable=(column.nullable != "N"),
            )
            for column in columns
        ),
        storage=StorageRoot(
            group_id=_required_int(storage_index.group_id, "storage group id"),
            file_no=_required_int(storage_index.root_file, "storage root file"),
            root_page=_required_int(storage_index.root_page, "storage root page"),
            scan_pages=scan_pages,
            storage_id=None if partition_objects else index_child.index_id,
            page_refs=page_refs if partition_objects else (),
        ),
    )
    group_data_files = tuple(
        item
        for item in files
        if item.group_id == _required_int(storage_index.group_id, "storage group id")
    )
    metadata = CalibratedMetadata(
        data_files=tuple(
            DataFileMeta(
                group_id=item.group_id,
                file_no=item.file_no_hint,
                path=item.path,
                page_size=item.page_size,
            )
            for item in group_data_files
        ),
        tables=(table,),
    )
    return OfflineTableResolution(
        metadata=metadata,
        table=table,
        system_file=system_file.path,
        table_object=table_object,
        table_object_id=table_object_id,
        index_child=index_child,
        storage_index=storage_index,
        columns=columns,
        schema_id=table_schema_id,
        control_file_data_files=database_summary.get("control_file_data_files"),
        segment_root=segment_root,
        partition_objects=partition_objects,
        partition_index_children=partition_index_children,
    )


def _select_system_file(files: list[DiscoveredDataFile]) -> DiscoveredDataFile:
    for item in files:
        if item.is_system_candidate:
            return item
    raise OfflineResolveError("SYSTEM.DBF candidate not found")


def _select_data_file(
    files: list[DiscoveredDataFile],
    *,
    group_id: int,
    file_no: int,
) -> DiscoveredDataFile:
    for item in files:
        if item.group_id == group_id and item.file_no_hint == file_no:
            return item
    raise OfflineResolveError(f"data file not found for group={group_id}, file={file_no}")


def _select_table_object(
    candidates: list[SysObjectCandidate],
    *,
    table_name: str,
) -> SysObjectCandidate:
    usable = [
        item
        for item in candidates
        if item.preferred_object_ids and item.has_schobj
    ]
    if not usable:
        raise OfflineResolveError(f"table object not found: {table_name}")
    return sorted(usable, key=lambda item: (-item.score, item.offset))[0]


def _select_table_object_id(candidate: SysObjectCandidate) -> int:
    for value in candidate.preferred_object_ids:
        if 10_000 <= value <= 60_000:
            return value
    raise OfflineResolveError(f"no preferred object id for {candidate.name}")


def _select_owner_table_object_row(
    system_file: Path,
    *,
    table_name: str,
    owner_name: str,
    page_size: int,
    sysobject_rows: tuple[SysObjectRowCandidate, ...] | None = None,
) -> SysObjectRowCandidate | None:
    rows = list(
        sysobject_rows
        if sysobject_rows is not None
        else _cached_sysobject_rows_for_file(system_file, page_size=page_size)
    )
    schema_id = _schema_id_for_owner(rows, owner_name)
    if schema_id is None:
        return None
    usable = [
        row
        for row in rows
        if row.type_name == "SCHOBJ"
        and row.subtype_name in {"UTAB", "STAB"}
        and row.object_id is not None
        and row.name.upper() == table_name.upper()
        and row.schema_id == schema_id
    ]
    if not usable:
        return None
    return sorted(usable, key=lambda item: (-item.score, item.offset))[0]


def _select_leaf_partition_object_rows(
    rows: tuple[SysObjectRowCandidate, ...],
    *,
    parent_object_id: int,
    schema_id: int | None,
) -> tuple[SysObjectRowCandidate, ...]:
    children_by_parent: dict[int, list[SysObjectRowCandidate]] = {}
    for row in rows:
        if (
            row.type_name == "SCHOBJ"
            and row.subtype_name in {"UTAB", "STAB"}
            and row.object_id is not None
            and row.parent_id is not None
            and (schema_id is None or row.schema_id == schema_id)
        ):
            children_by_parent.setdefault(row.parent_id, []).append(row)

    leaves: list[SysObjectRowCandidate] = []

    def visit(object_id: int) -> None:
        children = children_by_parent.get(object_id, [])
        if not children:
            return
        for child in children:
            child_id = _required_int(child.object_id, "partition object id")
            if children_by_parent.get(child_id):
                visit(child_id)
            else:
                leaves.append(child)

    visit(parent_object_id)
    partitions = [
        row
        for row in leaves
        if row.parent_id is not None
    ]
    best_by_id: dict[int, SysObjectRowCandidate] = {}
    for row in partitions:
        object_id = _required_int(row.object_id, "partition object id")
        current = best_by_id.get(object_id)
        if current is None or (row.score, -row.offset) > (current.score, -current.offset):
            best_by_id[object_id] = row
    return tuple(best_by_id[key] for key in sorted(best_by_id))


def _index_child_candidates_from_sysobject_rows(
    rows: tuple[SysObjectRowCandidate, ...],
    *,
    parent_object_id: int,
) -> list[SysObjectIndexChildCandidate]:
    candidates: list[SysObjectIndexChildCandidate] = []
    for row in rows:
        if row.type_name != "TABOBJ" or row.subtype_name != "INDEX":
            continue
        if row.parent_id != parent_object_id or row.object_id is None:
            continue
        candidates.append(
            SysObjectIndexChildCandidate(
                parent_object_id=parent_object_id,
                index_id=row.object_id,
                name=row.name,
                offset=row.offset,
                page_no=row.page_no,
                page_offset=row.page_offset,
                score=row.score + 50,
                type_name=row.type_name,
                name_offset=0,
                index_id_offset=None,
            )
        )
    return candidates


def _cached_sysobject_rows_for_file(
    system_file: Path,
    *,
    page_size: int,
) -> tuple[SysObjectRowCandidate, ...]:
    stat = system_file.stat()
    return _cached_sysobject_rows(
        str(system_file),
        page_size,
        stat.st_mtime_ns,
        stat.st_size,
    )


@lru_cache(maxsize=8)
def _cached_sysobject_rows(
    system_file: str,
    page_size: int,
    mtime_ns: int,
    size: int,
) -> tuple[SysObjectRowCandidate, ...]:
    del mtime_ns, size
    return tuple(dump_sysobject_rows(Path(system_file), page_size=page_size))


def _analyze_storage_roots(
    *,
    files: list[DiscoveredDataFile],
    storage_indexes: tuple[SysIndexCandidate, ...],
    page_size: int,
) -> dict[str, object]:
    roots: list[dict[str, object]] = []
    for storage_index in storage_indexes:
        data_file = _select_data_file(
            files,
            group_id=_required_int(storage_index.group_id, "storage group id"),
            file_no=_required_int(storage_index.root_file, "storage root file"),
        )
        known_file_nos = {
            item.file_no_hint
            for item in files
            if item.group_id == data_file.group_id
        }
        root = analyze_segment_root(
            path=data_file.path,
            page_size=data_file.page_size,
            group_id=_required_int(storage_index.group_id, "storage group id"),
            file_no=_required_int(storage_index.root_file, "storage root file"),
            root_page=_required_int(storage_index.root_page, "storage root page"),
            known_file_nos=known_file_nos,
        )
        root["storage_index_id"] = storage_index.index_id
        roots.append(root)
    if len(roots) == 1:
        return roots[0]
    return {"partition_roots": roots}


def _schema_id_for_owner(
    rows: list[SysObjectRowCandidate],
    owner_name: str,
) -> int | None:
    normalized_owner = owner_name.upper()
    builtin = BUILTIN_SCHEMA_IDS_BY_OWNER.get(normalized_owner)
    if builtin is not None:
        return builtin
    candidates = [
        row.object_id
        for row in rows
        if row.type_name == "SCH"
        and row.object_id is not None
        and row.name.upper() == normalized_owner
        and 0x09000000 <= row.object_id <= 0x09FFFFFF
    ]
    if not candidates:
        return None
    return sorted(candidates)[0]


def _table_object_candidate_from_row(row: SysObjectRowCandidate) -> SysObjectCandidate:
    object_id = _required_int(row.object_id, "table object id")
    return SysObjectCandidate(
        name=row.name,
        offset=row.offset,
        page_no=row.page_no,
        page_offset=row.page_offset,
        score=row.score,
        object_ids=(object_id,),
        likely_object_ids=(object_id,),
        preferred_object_ids=(object_id,),
        has_schobj=True,
        has_utab=row.subtype_name == "UTAB",
    )


def _select_columns(
    candidates: list[SysColumnCandidate],
) -> tuple[SysColumnCandidate, ...]:
    usable = [item for item in candidates if item.column_id is not None]
    if not usable:
        raise OfflineResolveError("table columns not found")
    best_by_column_id: dict[int, SysColumnCandidate] = {}
    for item in usable:
        column_id = _required_int(item.column_id, "column id")
        current = best_by_column_id.get(column_id)
        if current is None or (item.score, -item.offset) > (current.score, -current.offset):
            best_by_column_id[column_id] = item
    selected = tuple(best_by_column_id[key] for key in sorted(best_by_column_id))
    expected = tuple(range(len(selected)))
    observed = tuple(_required_int(item.column_id, "column id") for item in selected)
    if observed != expected:
        raise OfflineResolveError(
            "table columns are not a contiguous zero-based sequence: "
            f"observed={observed}, expected={expected}"
        )
    return selected


def _select_index_child(
    candidates: list[SysObjectIndexChildCandidate],
) -> SysObjectIndexChildCandidate:
    if not candidates:
        raise OfflineResolveError("table storage index child not found")
    return sorted(candidates, key=lambda item: (-item.score, item.offset))[0]


def _select_storage_index(candidates: list[SysIndexCandidate]) -> SysIndexCandidate:
    usable = [
        item
        for item in candidates
        if item.group_id is not None
        and item.root_file is not None
        and item.root_page is not None
        and item.type_name == "BT"
    ]
    if not usable:
        raise OfflineResolveError("SYSINDEXES storage root not found")
    return sorted(usable, key=lambda item: (-item.score, item.offset))[0]


def _required_int(value: int | None, name: str) -> int:
    if value is None:
        raise OfflineResolveError(f"missing {name}")
    return value


def _control_file_entries_for_path(
    *,
    control_file_data_files: dict[str, object] | None,
    path: Path,
) -> list[dict[str, object]]:
    if not control_file_data_files:
        return []
    entries = control_file_data_files.get("entries", [])
    if not isinstance(entries, list):
        return []
    target = str(path)
    matches: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matched_paths = entry.get("matched_paths", [])
        if not isinstance(matched_paths, list) or target not in matched_paths:
            continue
        matches.append(
            {
                "control_file": entry.get("control_file"),
                "control_file_ordinal": entry.get("control_file_ordinal"),
                "text": entry.get("text"),
                "normalized_path": entry.get("normalized_path"),
                "basename": entry.get("basename"),
                "offset": entry.get("offset"),
            }
        )
    return matches


def _manifest_diagnostics(
    *,
    data_files: list[dict[str, object]],
    segment_root: dict[str, object] | None,
) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    missing = [
        {
            "group_id": item.get("group_id"),
            "file_no": item.get("file_no"),
            "path": item.get("path"),
        }
        for item in data_files
        if not item.get("control_file_entries")
    ]
    if missing:
        diagnostics.append(
            {
                "level": "warning",
                "code": "segment-manifest-data-file-without-control-entry",
                "message": "one or more segment data files have no matched dm.ctl DBF occurrence evidence",
                "count": len(missing),
                "data_files": missing,
            }
        )
    if isinstance(segment_root, dict):
        segment_diagnostics = segment_root.get("diagnostics", [])
        if isinstance(segment_diagnostics, list):
            diagnostics.extend(
                item for item in segment_diagnostics if isinstance(item, dict)
            )
    return diagnostics


def _split_table_name(table_name: str, *, owner: str | None) -> tuple[str, str]:
    if "." in table_name:
        parsed_owner, parsed_name = table_name.split(".", 1)
        return parsed_owner.upper(), parsed_name.upper()
    return (owner or "SYSDBA").upper(), table_name.upper()
