from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from dmdul.bootstrap import (
    DICT_FIELDNAMES,
    DM_BUILTIN_SCHEMA_NAMES_BY_ID,
    _dictionary_rows_from_existing_dicts,
    _leaf_partition_objects_for_table,
    _owner_for_schema_id,
    _system_dictionary_storage_entry,
)
from dmdul.sysdict import SysObjectRowCandidate


class BootstrapTest(unittest.TestCase):
    def test_owner_for_schema_id_uses_full_builtin_schema_id(self) -> None:
        owners = dict(DM_BUILTIN_SCHEMA_NAMES_BY_ID)

        self.assertEqual(_owner_for_schema_id(0x09000001, owners), "SYSDBA")
        self.assertIsNone(_owner_for_schema_id(1, owners))

    def test_filters_requested_tables_from_existing_dict_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dict_dir = Path(tmp_dir)
            _write_dict(
                dict_dir / "user.dict",
                DICT_FIELDNAMES["user.dict"],
                [
                    {"dict_type": "user", "owner": "SYSDBA", "schema_id": "150994945"},
                    {"dict_type": "user", "owner": "TEST", "schema_id": "150995949"},
                ],
            )
            _write_dict(
                dict_dir / "tab.dict",
                DICT_FIELDNAMES["tab.dict"],
                [
                    {
                        "dict_type": "table",
                        "object_kind": "table",
                        "owner": "SYSDBA",
                        "name": "DUP",
                        "qualified_name": "SYSDBA.DUP",
                        "object_id": "34019",
                        "schema_id": "150994945",
                        "storage_index_id": "33595830",
                        "group_id": "6",
                        "root_file": "0",
                        "root_page": "1280",
                    },
                    {
                        "dict_type": "table",
                        "object_kind": "table",
                        "owner": "TEST",
                        "name": "DUP",
                        "qualified_name": "TEST.DUP",
                        "object_id": "34020",
                        "schema_id": "150995949",
                        "storage_index_id": "33595831",
                        "group_id": "6",
                        "root_file": "0",
                        "root_page": "1296",
                    },
                ],
            )
            _write_dict(
                dict_dir / "col.dict",
                DICT_FIELDNAMES["col.dict"],
                [
                    {
                        "dict_type": "column",
                        "owner": "SYSDBA",
                        "table_name": "DUP",
                        "qualified_table_name": "SYSDBA.DUP",
                        "object_id": "34019",
                        "column_id": "0",
                        "ordinal": "1",
                        "name": "ID",
                        "type_name": "INT",
                    },
                    {
                        "dict_type": "column",
                        "owner": "TEST",
                        "table_name": "DUP",
                        "qualified_table_name": "TEST.DUP",
                        "object_id": "34020",
                        "column_id": "0",
                        "ordinal": "1",
                        "name": "ID",
                        "type_name": "INT",
                    },
                ],
            )

            users, tables, columns, diagnostics = _dictionary_rows_from_existing_dicts(
                source_dict_dir=dict_dir,
                tables=("SYSDBA.DUP", "TEST.DUP"),
                owner=None,
            )

        self.assertEqual(diagnostics, [])
        self.assertEqual([(row["owner"], row["schema_id"]) for row in users], [("SYSDBA", "150994945"), ("TEST", "150995949")])
        self.assertEqual([(row["owner"], row["object_id"], row["root_page"]) for row in tables], [("SYSDBA", "34019", "1280"), ("TEST", "34020", "1296")])
        self.assertEqual([(row["owner"], row["object_id"], row["name"]) for row in columns], [("SYSDBA", "34019", "ID"), ("TEST", "34020", "ID")])

    def test_system_dictionary_storage_entry_prefers_discovered_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            storage_id = 33554540
            page0 = bytearray(_header_page(page_no=0, kind=0x13, storage_id=12))
            page0[0x80:0x84] = (1).to_bytes(4, "little")
            path.write_bytes(
                bytes(page0)
                + _header_page(page_no=1, kind=0x15, storage_id=storage_id)
            )
            progress: list[str] = []

            entry = _system_dictionary_storage_entry(
                path,
                object_id=0,
                page_size=8192,
                progress=progress.append,
            )

        self.assertEqual(entry["root_page"], 1)
        self.assertEqual(entry["storage_id"], storage_id)
        self.assertTrue(any("SYSOBJECTS root discovered from SYSTEM file header" in item for item in progress))

    def test_leaf_partition_objects_tolerates_cycles(self) -> None:
        table = _sysobject_row("T", 100, None)
        p1 = _sysobject_row("P1", 101, 100)
        p2 = _sysobject_row("P2", 102, 101)
        p1_cycle = _sysobject_row("P1", 101, 102)
        self_cycle = _sysobject_row("SELF", 103, 103)
        p3 = _sysobject_row("P3", 104, 100)

        leaves = _leaf_partition_objects_for_table(
            table_objects=[table, p1, p2, p1_cycle, self_cycle, p3],
            table=table,
        )

        self.assertEqual([(item.object_id, item.name) for item in leaves], [(104, "P3")])


def _write_dict(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _header_page(*, page_no: int, kind: int, storage_id: int) -> bytes:
    page = bytearray(8192)
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = b"\xff" * 6
    page[14:20] = b"\xff" * 6
    page[20:24] = kind.to_bytes(4, "little")
    page[58:62] = storage_id.to_bytes(4, "little")
    return bytes(page)


def _sysobject_row(
    name: str,
    object_id: int,
    parent_id: int | None,
) -> SysObjectRowCandidate:
    return SysObjectRowCandidate(
        name=name,
        object_id=object_id,
        schema_id=0x09000001,
        parent_id=parent_id,
        type_name="SCHOBJ",
        subtype_name="UTAB",
        offset=object_id,
        page_no=0,
        page_offset=object_id,
        score=150,
        source="test",
    )


if __name__ == "__main__":
    unittest.main()
