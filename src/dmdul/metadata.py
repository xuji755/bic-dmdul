from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    type_name: str
    length: int | None = None
    scale: int | None = None
    nullable: bool = True


@dataclass(frozen=True)
class StoragePageRef:
    file_no: int
    page_no: int


@dataclass(frozen=True)
class StoragePartitionRef:
    name: str
    page_ref: StoragePageRef


@dataclass(frozen=True)
class StorageRoot:
    group_id: int
    file_no: int
    root_page: int
    scan_pages: int = 1
    storage_id: int | None = None
    page_numbers: tuple[int, ...] = ()
    page_refs: tuple[StoragePageRef, ...] = ()
    partition_page_refs: tuple[StoragePartitionRef, ...] = ()


@dataclass(frozen=True)
class TableMeta:
    owner: str
    name: str
    columns: tuple[ColumnMeta, ...]
    storage: StorageRoot
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.name


@dataclass(frozen=True)
class DataFileMeta:
    group_id: int
    file_no: int
    path: Path
    page_size: int = 8192


@dataclass(frozen=True)
class CalibratedMetadata:
    data_files: tuple[DataFileMeta, ...]
    tables: tuple[TableMeta, ...]


    @classmethod
    def from_dict_dir(cls, dict_dir: Path) -> "CalibratedMetadata":
        file_rows = _read_csv_dict(dict_dir / "file.dict")
        table_rows = _read_csv_dict(dict_dir / "tab.dict")
        column_rows = _read_csv_dict(dict_dir / "col.dict")
        data_files = tuple(
            DataFileMeta(
                group_id=int(row["group_id"]),
                file_no=int(row.get("file_no") or 0),
                path=Path(row["path"]),
                page_size=int(row.get("page_size") or 8192),
            )
            for row in file_rows
            if row.get("path") and row.get("group_id") not in {None, ""}
        )
        columns_by_object_id: dict[int, list[ColumnMeta]] = {}
        for row in column_rows:
            object_id = _optional_int(row.get("object_id"))
            if object_id is None:
                continue
            columns_by_object_id.setdefault(object_id, []).append(
                ColumnMeta(
                    name=str(row["name"]),
                    type_name=str(row["type_name"]).upper(),
                    length=_optional_int(row.get("length")),
                    scale=_optional_int(row.get("scale")),
                    nullable=_optional_bool(row.get("nullable"), default=True),
                )
            )
        tables: list[TableMeta] = []
        skipped_table_rows: list[dict[str, str]] = []
        table_by_owner_name: dict[tuple[str, str], TableMeta] = {}
        for row in table_rows:
            if row.get("object_kind") != "table":
                continue
            object_id = _optional_int(row.get("object_id"))
            group_id = _optional_int(row.get("group_id"))
            root_file = _optional_int(row.get("root_file"))
            root_page = _optional_int(row.get("root_page"))
            if object_id is None or group_id is None or root_file is None or root_page is None:
                skipped_table_rows.append(row)
                continue
            columns = tuple(columns_by_object_id.get(object_id, ()))
            page_refs = _csv_page_refs(row.get("page_refs"))
            partition_page_refs = _csv_partition_page_refs(
                names_value=row.get("partition_names"),
                refs=page_refs,
            )
            table = TableMeta(
                owner=str(row.get("owner") or ""),
                name=str(row["name"]),
                columns=columns,
                storage=StorageRoot(
                    group_id=group_id,
                    file_no=root_file,
                    root_page=root_page,
                    scan_pages=int(row.get("scan_pages") or 1),
                    storage_id=None if page_refs else _optional_int(row.get("storage_index_id")),
                    page_refs=page_refs,
                    partition_page_refs=partition_page_refs,
                ),
            )
            tables.append(table)
            table_by_owner_name[(table.owner.upper(), table.name.upper())] = table
        for row in skipped_table_rows:
            mapped = _huge_table_from_raux_storage(
                row,
                columns_by_object_id=columns_by_object_id,
                table_by_owner_name=table_by_owner_name,
            )
            if mapped is not None:
                tables.append(mapped)
        return cls(data_files=data_files, tables=tuple(tables))

    @classmethod
    def from_json_file(cls, path: Path) -> "CalibratedMetadata":
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return cls.from_dict(payload)

    @classmethod
    def from_segment_manifest_file(cls, path: Path) -> "CalibratedMetadata":
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return cls.from_segment_manifest(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalibratedMetadata":
        data_files = tuple(
            DataFileMeta(
                group_id=int(item["group_id"]),
                file_no=int(item["file_no"]),
                path=Path(item["path"]),
                page_size=int(item.get("page_size", 8192)),
            )
            for item in payload.get("data_files", [])
        )
        tables = tuple(_table_from_dict(item) for item in payload.get("tables", []))
        return cls(data_files=data_files, tables=tables)

    @classmethod
    def from_segment_manifest(cls, payload: dict[str, Any]) -> "CalibratedMetadata":
        owner, name = _split_qualified_name(str(payload["table"]))
        segment = payload["segment"]
        columns = tuple(
            ColumnMeta(
                name=str(column["name"]),
                type_name=str(column["type_name"]).upper(),
                length=_optional_int(column.get("length")),
                scale=_optional_int(column.get("scale")),
                nullable=_optional_bool(column.get("nullable"), default=True),
            )
            for column in payload.get("columns", [])
        )
        data_files = tuple(
            DataFileMeta(
                group_id=int(item["group_id"]),
                file_no=int(item["file_no"]),
                path=Path(item["path"]),
                page_size=int(item.get("page_size", 8192)),
            )
            for item in payload.get("data_files", [])
        )
        table = TableMeta(
            owner=owner,
            name=name,
            columns=columns,
            storage=StorageRoot(
                group_id=int(segment["group_id"]),
                file_no=int(segment["root_file"]),
                root_page=int(segment["root_page"]),
                scan_pages=int(segment.get("scan_pages", 1)),
                storage_id=_optional_int(segment.get("storage_index_id")),
                page_numbers=_segment_manifest_page_numbers(payload),
                page_refs=_segment_manifest_page_refs(payload),
            ),
        )
        return cls(data_files=data_files, tables=(table,))

    def find_table(self, qualified_name: str) -> TableMeta:
        normalized = qualified_name.upper()
        for table in self.tables:
            if table.qualified_name.upper() == normalized or table.name.upper() == normalized:
                return table
        raise KeyError(f"table not found in calibrated metadata: {qualified_name}")

    def find_data_file(self, group_id: int, file_no: int) -> DataFileMeta:
        for data_file in self.data_files:
            if data_file.group_id == group_id and data_file.file_no == file_no:
                return data_file
        raise KeyError(f"data file not found for group={group_id}, file={file_no}")


def _table_from_dict(item: dict[str, Any]) -> TableMeta:
    columns = tuple(
        ColumnMeta(
            name=str(column["name"]),
            type_name=str(column["type_name"]).upper(),
            length=_optional_int(column.get("length")),
            scale=_optional_int(column.get("scale")),
            nullable=_optional_bool(column.get("nullable"), default=True),
        )
        for column in item.get("columns", [])
    )
    storage = item["storage"]
    return TableMeta(
        owner=str(item["owner"]),
        name=str(item["name"]),
        columns=columns,
        storage=StorageRoot(
            group_id=int(storage["group_id"]),
            file_no=int(storage["file_no"]),
            root_page=int(storage["root_page"]),
            scan_pages=int(storage.get("scan_pages", 1)),
            storage_id=_optional_int(storage.get("storage_id") or storage.get("storage_index_id")),
            page_numbers=tuple(int(value) for value in storage.get("page_numbers", ())),
            page_refs=_storage_page_refs(storage),
            partition_page_refs=_storage_partition_page_refs(storage),
        ),
    )


def _huge_table_from_raux_storage(
    row: dict[str, str],
    *,
    columns_by_object_id: dict[int, list[ColumnMeta]],
    table_by_owner_name: dict[tuple[str, str], TableMeta],
) -> TableMeta | None:
    object_id = _optional_int(row.get("object_id"))
    if object_id is None:
        return None
    columns = tuple(columns_by_object_id.get(object_id, ()))
    if not columns:
        return None
    owner = str(row.get("owner") or "")
    name = str(row.get("name") or "")
    if not owner or not name or "$" in name:
        return None
    raux = table_by_owner_name.get((owner.upper(), f"{name}$RAUX".upper()))
    if raux is None:
        return None
    return TableMeta(
        owner=owner,
        name=name,
        columns=columns,
        storage=raux.storage,
        diagnostics=(
            {
                "level": "warning",
                "code": "huge-raux-proxy-mapping",
                "message": (
                    "HUGE table was mapped to the internal $RAUX storage. "
                    "This recovers rows present in $RAUX, but does not decode "
                    "compressed column sections referenced by $AUX."
                ),
                "raux_table": raux.qualified_name,
            },
        ),
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _split_qualified_name(value: str) -> tuple[str, str]:
    if "." in value:
        owner, name = value.split(".", 1)
        return owner.upper(), name.upper()
    return "SYSDBA", value.upper()


def _segment_manifest_page_numbers(payload: dict[str, Any]) -> tuple[int, ...]:
    segment = payload["segment"]
    root_file = int(segment["root_file"])
    root_page = int(segment["root_page"])
    segment_root = payload.get("segment_root")
    if not isinstance(segment_root, dict):
        return ()
    pages: list[int] = []
    data_pages: list[int] = []
    for item in segment_root.get("candidate_page_refs", []):
        if not isinstance(item, dict):
            continue
        if int(item.get("file_no", -1)) != root_file:
            continue
        if item.get("target_page_kind_label") != "tentative-btree-data":
            continue
        data_pages.append(int(item["page_no"]))
    if _include_root_page_from_segment_manifest(segment_root, data_pages):
        pages.append(root_page)
    pages.extend(data_pages)
    return tuple(dict.fromkeys(pages))


def _segment_manifest_page_refs(payload: dict[str, Any]) -> tuple[StoragePageRef, ...]:
    segment = payload["segment"]
    root_file = int(segment["root_file"])
    root_page = int(segment["root_page"])
    segment_root = payload.get("segment_root")
    if not isinstance(segment_root, dict):
        return ()
    refs: list[StoragePageRef] = []
    data_refs: list[StoragePageRef] = []
    for item in segment_root.get("candidate_page_refs", []):
        if not isinstance(item, dict):
            continue
        if item.get("target_page_kind_label") != "tentative-btree-data":
            continue
        data_refs.append(
            StoragePageRef(
                file_no=int(item["file_no"]),
                page_no=int(item["page_no"]),
            )
        )
    if _include_root_page_from_segment_manifest(segment_root, data_refs):
        refs.append(StoragePageRef(file_no=root_file, page_no=root_page))
    refs.extend(data_refs)
    return tuple(dict.fromkeys(refs))


def _include_root_page_from_segment_manifest(
    segment_root: dict[str, Any],
    data_refs: list[Any],
) -> bool:
    root_header = segment_root.get("root_header")
    if not data_refs:
        return True
    if not isinstance(root_header, dict):
        return True
    return root_header.get("page_kind_label") == "tentative-btree-data"


def _storage_page_refs(storage: dict[str, Any]) -> tuple[StoragePageRef, ...]:
    if "page_refs" in storage:
        return tuple(
            StoragePageRef(
                file_no=int(item["file_no"]),
                page_no=int(item["page_no"]),
            )
            for item in storage["page_refs"]
        )
    file_no = int(storage["file_no"])
    return tuple(
        StoragePageRef(file_no=file_no, page_no=int(page_no))
        for page_no in storage.get("page_numbers", ())
    )


def _storage_partition_page_refs(storage: dict[str, Any]) -> tuple[StoragePartitionRef, ...]:
    partition_refs = storage.get("partition_page_refs")
    if not partition_refs:
        return ()
    refs: list[StoragePartitionRef] = []
    for item in partition_refs:
        page_ref = StoragePageRef(
            file_no=int(item["file_no"]),
            page_no=int(item["page_no"]),
        )
        refs.append(StoragePartitionRef(name=str(item["name"]), page_ref=page_ref))
    return tuple(refs)


def _csv_page_refs(value: Any) -> tuple[StoragePageRef, ...]:
    if value is None or value == "":
        return ()
    refs: list[StoragePageRef] = []
    for item in str(value).split(";"):
        if not item:
            continue
        file_no, page_no = item.split(":", 1)
        refs.append(StoragePageRef(file_no=int(file_no), page_no=int(page_no)))
    return tuple(refs)


def _csv_partition_page_refs(
    *,
    names_value: Any,
    refs: tuple[StoragePageRef, ...],
) -> tuple[StoragePartitionRef, ...]:
    if names_value is None or names_value == "":
        return ()
    names = [item for item in str(names_value).split(";") if item]
    if len(names) != len(refs):
        return ()
    return tuple(
        StoragePartitionRef(name=name, page_ref=page_ref)
        for name, page_ref in zip(names, refs)
    )


def _read_csv_dict(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
