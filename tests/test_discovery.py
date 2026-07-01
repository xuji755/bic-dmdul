from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dmdul.discovery import discover_data_files, find_control_files, find_dbf_files


def _page0(group_raw: int, page_kind: int = 0x13) -> bytes:
    page = bytearray(b"\0" * 8192)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[8:20] = b"\xff" * 12
    page[20:24] = page_kind.to_bytes(4, "little")
    return bytes(page)


class DiscoverDataFilesTest(unittest.TestCase):
    def test_discovers_dbf_files_and_system_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            system = root / "SYSTEM.DBF"
            temp = root / "TEMP.DBF"
            user = root / "DMDUL_TS01.DBF"
            user2 = root / "main2.dbf"
            ignored = root / "README.txt"
            system.write_bytes(_page0(0) + (b"\0" * 8192))
            temp.write_bytes(_page0(0, page_kind=0) + (b"\0" * 8192))
            user.write_bytes(_page0(6) + (b"\0" * 8192 * 3))
            user2.write_bytes(_page0(0x00010004) + (b"\0" * 8192))
            ignored.write_text("not a data file", encoding="utf-8")

            files = discover_data_files(root)

        self.assertEqual([item.group_id for item in files], [0, 0, 4, 6])
        self.assertEqual([item.page_type_raw for item in files], [0, 0, 4, 6])
        self.assertEqual([item.file_no_hint for item in files], [0, 0, 1, 0])
        self.assertEqual([item.pages for item in files], [2, 2, 2, 4])
        self.assertTrue(files[0].is_system_candidate)
        self.assertFalse(files[1].is_system_candidate)
        self.assertFalse(files[2].is_system_candidate)

    def test_find_dbf_files_includes_short_dbf_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "SHORT.DBF").write_bytes(b"x")
            (root / "README.txt").write_text("ignored", encoding="utf-8")

            files = find_dbf_files(root)

        self.assertEqual([item.name for item in files], ["SHORT.DBF"])

    def test_find_control_files_includes_dm_ctl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dm.ctl").write_bytes(b"control")
            (root / "nested").mkdir()
            (root / "nested" / "copy.CTL").write_bytes(b"control")
            (root / "README.txt").write_text("ignored", encoding="utf-8")

            files = find_control_files(root)

        self.assertEqual(sorted(item.name for item in files), ["copy.CTL", "dm.ctl"])


if __name__ == "__main__":
    unittest.main()
