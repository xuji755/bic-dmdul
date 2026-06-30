import tempfile
import unittest
from pathlib import Path

from dmdul.sysdict import (
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)


class SysDictHeuristicTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
