import tempfile
import unittest
from pathlib import Path

from dmdul.control_file import compare_control_files, summarize_control_file


class ControlFileTest(unittest.TestCase):
    def test_summarize_control_file_records_path_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(
                b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00"
                b"TS01=/dmdata/data/DAMENG/TS01.DBF\x00"
            )

            summary = summarize_control_file(path, sample_limit=8)

        self.assertEqual(
            summary["dbf_path_hints"],
            [
                "/dmdata/data/DAMENG/SYSTEM.DBF",
                "/dmdata/data/DAMENG/TS01.DBF",
            ],
        )
        self.assertEqual(summary["dbf_path_hint_records"][0]["offset"], 10)
        self.assertEqual(summary["dbf_path_hint_records"][0]["length"], 30)
        self.assertEqual(summary["dbf_path_hint_records"][0]["basename"], "system.dbf")
        self.assertEqual(
            summary["dbf_path_hint_records"][0]["normalized_path"],
            "/dmdata/data/dameng/system.dbf",
        )
        self.assertEqual(
            [item["ordinal"] for item in summary["dbf_path_occurrences"]],
            [0, 1],
        )
        self.assertEqual(summary["printable_string_records"][0]["offset"], 1)

    def test_summarize_control_file_keeps_duplicate_dbf_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(
                b"\x00A=C:\\DM\\DATA\\MAIN01.DBF\x00"
                b"B=/dm/data/main01.dbf\x00"
                b"C=C:\\DM\\DATA\\MAIN01.DBF\x00"
            )

            summary = summarize_control_file(path, sample_limit=8)

        self.assertEqual(
            summary["dbf_path_hints"],
            ["C:\\DM\\DATA\\MAIN01.DBF", "/dm/data/main01.dbf"],
        )
        self.assertEqual(len(summary["dbf_path_occurrences"]), 3)
        self.assertEqual(
            [item["basename"] for item in summary["dbf_path_occurrences"]],
            ["main01.dbf", "main01.dbf", "main01.dbf"],
        )
        self.assertEqual(
            [item["ordinal"] for item in summary["dbf_path_occurrences"]],
            [0, 1, 2],
        )

    def test_summarize_control_file_respects_zero_sample_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00")

            summary = summarize_control_file(path, sample_limit=0)

        self.assertEqual(summary["dbf_path_hints"], [])
        self.assertEqual(summary["dbf_path_hint_records"], [])
        self.assertEqual(summary["dbf_path_occurrences"], [])
        self.assertEqual(summary["printable_string_records"], [])

    def test_compare_control_files_reports_changed_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            before = Path(tmp_dir) / "before.ctl"
            after = Path(tmp_dir) / "after.ctl"
            before.write_bytes(b"aaaabbbbcccc")
            after.write_bytes(b"aaaaBBbbccccXX")

            comparison = compare_control_files(
                before,
                after,
                context_bytes=2,
                sample_limit=8,
            )

        self.assertFalse(comparison["same_size"])
        self.assertEqual(comparison["changed_bytes"], 4)
        self.assertEqual(comparison["changed_ranges_total"], 2)
        self.assertEqual(comparison["changed_ranges"][0]["start"], 4)
        self.assertEqual(comparison["changed_ranges"][0]["stop_exclusive"], 6)
        self.assertEqual(comparison["changed_ranges"][0]["before_hex"], b"aabbbb".hex())
        self.assertEqual(comparison["changed_ranges"][0]["after_hex"], b"aaBBbb".hex())
        numeric_candidates = comparison["changed_ranges"][0]["numeric_candidates"]
        self.assertTrue(
            any(
                item["offset"] == 4
                and item["size"] == 2
                and item["endian"] == "little"
                for item in numeric_candidates
            )
        )
        self.assertEqual(comparison["changed_ranges"][1]["start"], 12)
        self.assertEqual(comparison["changed_ranges"][1]["after_hex"], b"ccXX".hex())

    def test_compare_control_files_respects_zero_sample_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            before = Path(tmp_dir) / "before.ctl"
            after = Path(tmp_dir) / "after.ctl"
            before.write_bytes(b"abc")
            after.write_bytes(b"axc")

            comparison = compare_control_files(before, after, sample_limit=0)

        self.assertEqual(comparison["changed_bytes"], 1)
        self.assertEqual(comparison["changed_ranges_total"], 1)
        self.assertEqual(comparison["changed_ranges"], [])


if __name__ == "__main__":
    unittest.main()
