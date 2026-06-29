import tempfile
import unittest
from pathlib import Path

from dmdul.storage import DataFile


class DataFileTest(unittest.TestCase):
    def test_find_marker_reports_page_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.dbf"
            path.write_bytes((b"\0" * 32) + b"DMDUL_ROW_0001" + (b"\0" * 64))

            matches = list(DataFile(path, page_size=16).find(b"DMDUL_ROW_0001"))

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].offset, 32)
        self.assertEqual(matches[0].page_no, 2)
        self.assertEqual(matches[0].page_offset, 0)


if __name__ == "__main__":
    unittest.main()
