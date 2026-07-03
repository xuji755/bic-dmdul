from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dmdul.sysdict import (
    discover_system_dictionary_root_from_file_header,
    discover_storage_root_page,
    dump_syscolumn_rows,
    dump_sysindex_rows,
    dump_sysobject_rows,
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)


class SysDictHeuristicTest(unittest.TestCase):
    def test_discovers_system_dictionary_root_from_file_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            storage_id = 33554540
            page0 = bytearray(_header_page(page_no=0, kind=0x13, storage_id=12))
            page0[0x80:0x84] = (2).to_bytes(4, "little")
            path.write_bytes(
                bytes(page0)
                + _header_page(page_no=1, kind=0x14, storage_id=33554434)
                + _header_page(page_no=2, kind=0x15, storage_id=storage_id)
            )

            candidate = discover_system_dictionary_root_from_file_header(
                path,
                object_id=0,
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.root_page, 2)
        self.assertEqual(candidate.storage_id, storage_id)
        self.assertIn("system-page0-offset-0x80", candidate.source)

    def test_discovers_storage_root_page_from_page_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            storage_id = 33554540
            path.write_bytes(
                _header_page(page_no=0, kind=0x13, storage_id=0)
                + _header_page(page_no=1, kind=0x14, storage_id=storage_id, next_page=2)
                + _header_page(page_no=2, kind=0x15, storage_id=storage_id)
                + _header_page(page_no=3, kind=0x15, storage_id=storage_id, prev_page=2)
            )

            candidate = discover_storage_root_page(path, storage_id=storage_id)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.root_page, 2)
        self.assertEqual(candidate.page_kind_raw, 0x15)
        self.assertIn("root-shape", candidate.source)

    def test_finds_sysobject_like_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            payload = (
                b"\0" * 8192
                + b"N" * 80
                + (999999).to_bytes(4, "little")
                + b"\0" * 40
                + (33630).to_bytes(4, "little")
                + b"\0" * 16
                + bytes([0x8A])
                + b"DMDUL_ONE2"
                + bytes([0x86])
                + b"SCHOBJ"
                + bytes([0x84])
                + b"UTAB"
                + b"\0" * 128
            )
            path.write_bytes(payload)

            candidates = find_sysobject_candidates(path, "DMDUL_ONE2")

        self.assertTrue(candidates)
        best = candidates[0]
        self.assertEqual(best.name, "DMDUL_ONE2")
        self.assertEqual(best.page_no, 1)
        self.assertGreaterEqual(best.score, 50)
        self.assertIn(33630, best.object_ids)
        self.assertIn(33630, best.likely_object_ids)
        self.assertIn(33630, best.preferred_object_ids)
        self.assertLess(best.object_ids.index(33630), 5)
        self.assertTrue(best.has_schobj)
        self.assertTrue(best.has_utab)

    def test_dumps_sysobject_table_and_index_child_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            table_id = 33629
            index_id = 33595349
            index_name = f"INDEX{index_id}".encode("ascii")
            payload = (
                b"\0" * 8192
                + table_id.to_bytes(4, "little")
                + b"\0" * 16
                + bytes([0x8A])
                + b"DMDUL_MANY"
                + bytes([0x86])
                + b"SCHOBJ"
                + bytes([0x84])
                + b"UTAB"
                + b"\0" * 64
                + index_id.to_bytes(4, "little")
                + b"\0" * 12
                + table_id.to_bytes(4, "little")
                + b"\0" * 8
                + bytes([0x86])
                + b"TABOBJ"
                + bytes([0x80 + len(index_name)])
                + index_name
                + bytes([0x85])
                + b"INDEX"
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        by_name = {row.name: row for row in rows}
        self.assertEqual(by_name["DMDUL_MANY"].object_id, table_id)
        self.assertEqual(by_name["DMDUL_MANY"].type_name, "SCHOBJ")
        self.assertEqual(by_name["DMDUL_MANY"].subtype_name, "UTAB")
        index_row = by_name[f"INDEX{index_id}"]
        self.assertEqual(index_row.object_id, index_id)
        self.assertEqual(index_row.parent_id, table_id)
        self.assertEqual(index_row.type_name, "TABOBJ")
        self.assertEqual(index_row.subtype_name, "INDEX")

    def test_dumps_sysobject_index_child_with_name_before_tabobj_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            table_id = 33629
            index_id = 33595349
            index_name = f"INDEX{index_id}".encode("ascii")
            payload = (
                b"\0" * 8192
                + b"\0" * 7
                + index_id.to_bytes(4, "little")
                + (0x09000001).to_bytes(4, "little")
                + table_id.to_bytes(4, "little")
                + b"\0" * 44
                + bytes([0x80 + len(index_name)])
                + index_name
                + bytes([0x86])
                + b"TABOBJ"
                + bytes([0x85])
                + b"INDEX"
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        row = {item.name: item for item in rows}[f"INDEX{index_id}"]
        self.assertEqual(row.object_id, index_id)
        self.assertEqual(row.parent_id, table_id)
        self.assertEqual(row.schema_id, 0x09000001)
        self.assertEqual(row.type_name, "TABOBJ")
        self.assertEqual(row.subtype_name, "INDEX")

    def test_dumps_named_system_index_child_from_slot_fixed_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            row = bytes.fromhex(
                "006f000c0ff00308000002000000090300000000000000e807d6ce848dfa02"
                "000000000000000000000000000000000000000000000000000000000000"
                "598e535953494e444558545553455253865441424f424a85494e4445582b"
                "0000000000005f010000a111771700000000"
            )
            path.write_bytes(b"\0" * 8192 + row)

            rows = dump_sysobject_rows(path)

        item = {row.name: row for row in rows}["SYSINDEXTUSERS"]
        self.assertEqual(item.object_id, 33554440)
        self.assertEqual(item.schema_id, 0x09000000)
        self.assertEqual(item.parent_id, 3)
        self.assertEqual(item.type_name, "TABOBJ")
        self.assertEqual(item.subtype_name, "INDEX")

    def test_dumps_sysobject_table_row_with_full_schema_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            table_id = 34018
            schema_id = 0x09000001
            payload = (
                b"\0" * 8192
                + table_id.to_bytes(4, "little")
                + schema_id.to_bytes(4, "little")
                + b"\xff" * 4
                + b"\0" * 8
                + bytes([0x8C])
                + b"DMDUL_TYPES3"
                + bytes([0x86])
                + b"SCHOBJ"
                + bytes([0x84])
                + b"UTAB"
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        row = {item.name: item for item in rows}["DMDUL_TYPES3"]
        self.assertEqual(row.object_id, table_id)
        self.assertEqual(row.schema_id, schema_id)

    def test_dumps_low_object_id_sysobject_table_row_from_slot_fixed_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            row = bytes.fromhex(
                "006800000cf00303000000000009ffffffff00000000e807d6ce848dfa02"
                "000000000000000000000000000000000000000000000000000000000000"
                "59885359535553455224865343484f424a84535441421d0000000000005f"
                "0100006409771700000000"
            )
            path.write_bytes(b"\0" * 8192 + row)

            rows = dump_sysobject_rows(path)

        item = {row.name: row for row in rows}["SYSUSER$"]
        self.assertEqual(item.object_id, 3)
        self.assertEqual(item.schema_id, 0x09000000)
        self.assertEqual(item.type_name, "SCHOBJ")
        self.assertEqual(item.subtype_name, "STAB")

    def test_dumps_low_object_id_sysobject_table_row_with_schema_at_plus_four(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            row = bytes.fromhex(
                "006800000cf0030300000000000009ffffffff00000000e807d6ce848dfa02"
                "000000000000000000000000000000000000000000000000000000000000"
                "59885359535553455224865343484f424a84535441421d0000000000005f"
                "0100006409771700000000"
            )
            path.write_bytes(b"\0" * 8192 + row)

            rows = dump_sysobject_rows(path)

        item = {row.name: row for row in rows}["SYSUSER$"]
        self.assertEqual(item.object_id, 3)
        self.assertEqual(item.schema_id, 0x09000000)

    def test_dumps_recent_user_table_row_from_slot_fixed_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            row = bytes.fromhex(
                "00a900000c00033185000001000009ffffffff00000000ea871bd12b41d1"
                "0c000020000000000000000100000010000000000000000000000000000000"
                "005991444d44554c5f545950455f434f56455235865343484f424a845554"
                "41428a06000000200500003800ac00000000000000000000000000000000"
                "000001000000000000000000000000000000000000000000000000000000"
                "00000000612801000000ffffffff7fffff698537040000"
            )
            path.write_bytes(b"\0" * 8192 + row)

            rows = dump_sysobject_rows(path)

        item = {row.name: row for row in rows}["DMDUL_TYPE_COVER5"]
        self.assertEqual(item.object_id, 34097)
        self.assertEqual(item.schema_id, 0x09000001)
        self.assertEqual(item.parent_id, None)
        self.assertEqual(item.type_name, "SCHOBJ")
        self.assertEqual(item.subtype_name, "UTAB")

    def test_dumps_partition_table_rows_with_parent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            parent_row = _sysobject_slot_table_row(
                table_id=34099,
                schema_id=0x09000001,
                parent_id=None,
                name="DMDUL_PART_T",
            )
            partition_row = _sysobject_slot_table_row(
                table_id=34100,
                schema_id=0x09000001,
                parent_id=34099,
                name="DMDUL_PART_T_P1",
            )
            path.write_bytes(b"\0" * 8192 + parent_row + partition_row)

            rows = dump_sysobject_rows(path)

        by_name = {row.name: row for row in rows}
        self.assertEqual(by_name["DMDUL_PART_T"].object_id, 34099)
        self.assertEqual(by_name["DMDUL_PART_T"].parent_id, None)
        self.assertEqual(by_name["DMDUL_PART_T_P1"].object_id, 34100)
        self.assertEqual(by_name["DMDUL_PART_T_P1"].parent_id, 34099)

    def test_dumps_schema_row_with_name_after_sch_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            schema_id = 0x090003ED
            payload = (
                b"\0" * 8192
                + b"\0" * 16
                + schema_id.to_bytes(4, "little")
                + b"\0" * 4
                + bytes([0x83])
                + b"SCH"
                + bytes([0x84])
                + b"TEST"
                + b"\0" * 32
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        row = {item.name: item for item in rows}["TEST"]
        self.assertEqual(row.object_id, schema_id)
        self.assertEqual(row.schema_id, schema_id)
        self.assertEqual(row.type_name, "SCH")

    def test_dumps_schema_row_with_name_before_sch_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            schema_id = 0x090003ED
            payload = (
                b"\0" * 8192
                + b"\0" * 7
                + schema_id.to_bytes(4, "little")
                + b"\0" * 53
                + bytes([0x84])
                + b"TEST"
                + bytes([0x83])
                + b"SCH"
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        row = {item.name: item for item in rows}["TEST"]
        self.assertEqual(row.object_id, schema_id)
        self.assertEqual(row.schema_id, schema_id)
        self.assertEqual(row.type_name, "SCH")

    def test_rejects_schema_row_with_out_of_range_full_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            false_schema_id = 0x534A5950
            payload = (
                b"\0" * 8192
                + false_schema_id.to_bytes(4, "little")
                + b"\0" * 4
                + bytes([0x83])
                + b"SCH"
                + bytes([0x85])
                + b"NOISE"
                + b"\0" * 32
            )
            path.write_bytes(payload)

            rows = dump_sysobject_rows(path)

        self.assertNotIn("NOISE", {item.name for item in rows})

    def test_finds_syscolumns_like_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            object_id = 33629
            payload = (
                b"\0" * 8192
                + object_id.to_bytes(4, "little")
                + (0).to_bytes(2, "little")
                + (4).to_bytes(4, "little")
                + b"\0" * 8
                + bytes([0x82])
                + b"ID"
                + bytes([0x83])
                + b"INT"
                + b"\0" * 12
                + object_id.to_bytes(4, "little")
                + (1).to_bytes(2, "little")
                + (64).to_bytes(4, "little")
                + b"\0" * 8
                + bytes([0x86])
                + b"MARKER"
                + bytes([0x87])
                + b"VARCHAR"
                + b"\0" * 12
                + object_id.to_bytes(4, "little")
                + (2).to_bytes(2, "little")
                + (3000).to_bytes(4, "little")
                + b"\0" * 8
                + bytes([0x83])
                + b"PAD"
                + bytes([0x87])
                + b"VARCHAR"
            )
            path.write_bytes(payload)

            candidates = find_syscolumn_candidates(path, object_id)

        by_name = {candidate.name: candidate for candidate in candidates}
        self.assertEqual(by_name["ID"].column_id, 0)
        self.assertEqual(by_name["ID"].length, 4)
        self.assertEqual(by_name["ID"].type_name, "INT")
        self.assertEqual(by_name["MARKER"].column_id, 1)
        self.assertEqual(by_name["MARKER"].length, 64)
        self.assertEqual(by_name["MARKER"].type_name, "VARCHAR")
        self.assertEqual(by_name["PAD"].column_id, 2)
        self.assertEqual(by_name["PAD"].length, 3000)
        self.assertEqual(by_name["PAD"].type_name, "VARCHAR")

    def test_dumps_syscolumns_keeps_same_column_name_for_different_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            first_id = 33629
            second_id = 33630
            path.write_bytes(
                b"\0" * 8192
                + _syscolumns_row(
                    object_id=first_id,
                    column_id=0,
                    length=4,
                    scale=0,
                    nullable="Y",
                    name="ID",
                    type_name="INT",
                )
                + _syscolumns_row(
                    object_id=second_id,
                    column_id=0,
                    length=4,
                    scale=0,
                    nullable="Y",
                    name="ID",
                    type_name="INT",
                )
            )

            rows = dump_syscolumn_rows(path)

        by_object = {row.object_id: row for row in rows}
        self.assertEqual(set(by_object), {first_id, second_id})
        self.assertEqual(by_object[first_id].name, "ID")
        self.assertEqual(by_object[second_id].name, "ID")

    def test_finds_syscolumns_from_calibrated_clean_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            object_id = 33712
            row = _syscolumns_row(
                object_id=object_id,
                column_id=1,
                length=1,
                scale=0,
                nullable="Y",
                name="C_TINY",
                type_name="TINYINT",
            )
            path.write_bytes(b"\0" * 8192 + row)

            candidates = find_syscolumn_candidates(path, object_id)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].column_id, 1)
        self.assertEqual(candidates[0].length, 1)
        self.assertEqual(candidates[0].scale, 0)
        self.assertEqual(candidates[0].nullable, "Y")
        self.assertEqual(candidates[0].name, "C_TINY")
        self.assertEqual(candidates[0].type_name, "TINYINT")
        self.assertEqual(candidates[0].score, 140)

    def test_finds_sysindex_like_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            index_id = 33595350
            payload = (
                b"\0" * 8192
                + index_id.to_bytes(4, "little")
                + b"N"
                + (6).to_bytes(2, "little")
                + (0).to_bytes(2, "little")
                + (144).to_bytes(4, "little")
                + b"BT"
                + (0).to_bytes(4, "little")
                + (1).to_bytes(4, "little")
                + b"\0" * 32
            )
            path.write_bytes(payload)

            candidates = find_sysindex_candidates(path, index_id)

        self.assertTrue(candidates)
        best = candidates[0]
        self.assertEqual(best.index_id, index_id)
        self.assertEqual(best.page_no, 1)
        self.assertEqual(best.is_unique, "N")
        self.assertEqual(best.group_id, 6)
        self.assertEqual(best.root_file, 0)
        self.assertEqual(best.root_page, 144)
        self.assertEqual(best.type_name, "BT")
        self.assertEqual(best.flag, 1)
        self.assertGreaterEqual(best.score, 80)

    def test_dumps_sysindex_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            first_id = 33595349
            second_id = 33595350
            payload = (
                b"\0" * 8192
                + _sysindex_row(first_id, root_page=80)
                + b"\0" * 32
                + _sysindex_row(second_id, root_page=144)
            )
            path.write_bytes(payload)

            rows = dump_sysindex_rows(path)

        by_id = {row.index_id: row for row in rows}
        self.assertEqual(by_id[first_id].root_page, 80)
        self.assertEqual(by_id[second_id].root_page, 144)
        self.assertEqual(by_id[first_id].type_name, "BT")

    def test_finds_sysobject_index_child_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "SYSTEM.DBF"
            parent_object_id = 33630
            index_id = 33595350
            payload = (
                b"\0" * 8192
                + index_id.to_bytes(4, "little")
                + b"\0" * 12
                + parent_object_id.to_bytes(4, "little")
                + b"\0" * 8
                + bytes([0x86])
                + b"TABOBJ"
                + bytes([0x8D])
                + b"INDEX33595350"
                + b"\0" * 32
            )
            path.write_bytes(payload)

            candidates = find_sysobject_index_child_candidates(
                path,
                parent_object_id,
            )

        self.assertTrue(candidates)
        best = candidates[0]
        self.assertEqual(best.parent_object_id, parent_object_id)
        self.assertEqual(best.index_id, index_id)
        self.assertEqual(best.name, "INDEX33595350")
        self.assertEqual(best.type_name, "TABOBJ")
        self.assertEqual(best.page_no, 1)
        self.assertIsNotNone(best.index_id_offset)
        self.assertGreaterEqual(best.score, 90)


def _sysindex_row(index_id: int, *, root_page: int) -> bytes:
    return (
        index_id.to_bytes(4, "little")
        + b"N"
        + (6).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + root_page.to_bytes(4, "little")
        + b"BT"
        + (0).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
    )


def _header_page(
    *,
    page_no: int,
    kind: int,
    storage_id: int,
    prev_page: int | None = None,
    next_page: int | None = None,
) -> bytes:
    page = bytearray(8192)
    page[0:4] = (0).to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = (
        b"\xff" * 6
        if prev_page is None
        else (0).to_bytes(2, "little") + prev_page.to_bytes(4, "little")
    )
    page[14:20] = (
        b"\xff" * 6
        if next_page is None
        else (0).to_bytes(2, "little") + next_page.to_bytes(4, "little")
    )
    page[20:24] = kind.to_bytes(4, "little")
    page[58:62] = storage_id.to_bytes(4, "little")
    return bytes(page)


def _syscolumns_row(
    *,
    object_id: int,
    column_id: int,
    length: int,
    scale: int,
    nullable: str,
    name: str,
    type_name: str,
) -> bytes:
    body = (
        bytes.fromhex("00000c")
        + object_id.to_bytes(4, "little")
        + column_id.to_bytes(2, "little")
        + length.to_bytes(4, "little")
        + scale.to_bytes(2, "little")
        + nullable.encode("ascii")
        + b"\0" * 4
        + bytes([0x80 + len(name)])
        + name.encode("ascii")
        + bytes([0x80 + len(type_name)])
        + type_name.encode("ascii")
        + bytes.fromhex("ac1500000000ffffffff7fffff30d734040000")
    )
    return (len(body) + 2).to_bytes(2, "big") + body


def _sysobject_slot_table_row(
    *,
    table_id: int,
    schema_id: int,
    parent_id: int | None,
    name: str,
) -> bytes:
    encoded_name = name.encode("ascii")
    body = (
        bytes.fromhex("000c3003")
        + table_id.to_bytes(4, "little")
        + schema_id.to_bytes(4, "little")
        + (0xFFFFFFFF if parent_id is None else parent_id).to_bytes(4, "little")
        + b"\0" * 44
        + bytes([0x80 + len(encoded_name)])
        + encoded_name
        + bytes([0x86])
        + b"SCHOBJ"
        + bytes([0x84])
        + b"UTAB"
        + bytes.fromhex("ac000000000000")
    )
    return (len(body) + 2).to_bytes(2, "big") + body


if __name__ == "__main__":
    unittest.main()
