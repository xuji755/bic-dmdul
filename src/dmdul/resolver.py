from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .discovery import DiscoveredDataFile, discover_data_files
from .database_summary import summarize_database_dir
from .metadata import (
    CalibratedMetadata,
    ColumnMeta,
    DataFileMeta,
    StorageRoot,
    TableMeta,
)
from .sysdict import (
    SysColumnCandidate,
    SysIndexCandidate,
    SysObjectCandidate,
    SysObjectIndexChildCandidate,
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)


class OfflineResolveError(RuntimeError):
    pass


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
    control_file_data_files: dict[str, object] | None = None

    def as_manifest(self) -> dict[str, object]:
        return {
            "system_file": str(self.system_file),
            "table": self.table.qualified_name,
            "table_object_id": self.table_object_id,
            "storage_index_id": self.index_child.index_id,
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
            "data_files": [
                {
                    "group_id": item.group_id,
                    "file_no": item.file_no,
                    "path": str(item.path),
                    "page_size": item.page_size,
                }
                for item in self.metadata.data_files
            ],
            "control_file_data_files": self.control_file_data_files,
            "mode": "dmctl-system-sysdict-segment-root",
        }


def resolve_offline_table_metadata(
    *,
    database_dir: Path,
    table_name: str,
    page_size: int = 8192,
    owner: str | None = None,
    scan_pages: int = 64,
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
    table_object = _select_table_object(
        find_sysobject_candidates(system_file.path, object_name, page_size=page_size),
        table_name=object_name,
    )
    table_object_id = _select_table_object_id(table_object)
    columns = _select_columns(
        find_syscolumn_candidates(system_file.path, table_object_id, page_size=page_size)
    )
    index_child = _select_index_child(
        find_sysobject_index_child_candidates(
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
    data_file = _select_data_file(
        files,
        group_id=_required_int(storage_index.group_id, "storage group id"),
        file_no=_required_int(storage_index.root_file, "storage root file"),
    )
    table = TableMeta(
        owner=owner_name,
        name=object_name,
        columns=tuple(
            ColumnMeta(
                name=column.name,
                type_name=column.type_name,
                length=column.length,
            )
            for column in columns
        ),
        storage=StorageRoot(
            group_id=_required_int(storage_index.group_id, "storage group id"),
            file_no=_required_int(storage_index.root_file, "storage root file"),
            root_page=_required_int(storage_index.root_page, "storage root page"),
            scan_pages=scan_pages,
        ),
    )
    metadata = CalibratedMetadata(
        data_files=(
            DataFileMeta(
                group_id=data_file.group_id,
                file_no=data_file.file_no_hint,
                path=data_file.path,
                page_size=data_file.page_size,
            ),
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
        control_file_data_files=database_summary.get("control_file_data_files"),
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


def _select_columns(
    candidates: list[SysColumnCandidate],
) -> tuple[SysColumnCandidate, ...]:
    usable = [item for item in candidates if item.column_id is not None]
    if not usable:
        raise OfflineResolveError("table columns not found")
    return tuple(sorted(usable, key=lambda item: item.column_id or 0))


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


def _split_table_name(table_name: str, *, owner: str | None) -> tuple[str, str]:
    if "." in table_name:
        parsed_owner, parsed_name = table_name.split(".", 1)
        return parsed_owner.upper(), parsed_name.upper()
    return (owner or "SYSDBA").upper(), table_name.upper()
