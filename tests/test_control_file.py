from __future__ import annotations

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

    def test_summarize_control_file_maps_tablespace_names_to_dbf_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(
                b"".join(
                    [
                        b"\x00DATAFILE=/dmdata/data/DAMENG/NO_TS.DBF\x00",
                        b"\x00SYSTEM\x00",
                        b"\x00" * 24,
                        b"/dmdata/data/DAMENG/SYSTEM.DBF\x00",
                        b"\x00DMDUL_TS\x00",
                        b"\x00NORMAL\x00",
                        b"\x00" * 32,
                        b"/dmdata/data/DAMENG/DMDUL_TS01.DBF\x00",
                        b"\x00" * 32,
                        b"/dmdata/data/DAMENG/DMDUL_TS02.DBF\x00",
                    ]
                )
            )

            summary = summarize_control_file(path, sample_limit=8)

        by_basename = {
            item["basename"]: item for item in summary["tablespace_file_hints"]
        }
        self.assertEqual(by_basename["system.dbf"]["tablespace_name"], "SYSTEM")
        self.assertEqual(
            by_basename["dmdul_ts01.dbf"]["tablespace_name"],
            "DMDUL_TS",
        )
        self.assertEqual(
            by_basename["dmdul_ts02.dbf"]["tablespace_name"],
            "DMDUL_TS",
        )
        self.assertNotIn("no_ts.dbf", by_basename)

    def test_summarize_control_file_sample_limit_only_caps_printable_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00")

            summary = summarize_control_file(path, sample_limit=0)

        self.assertEqual(summary["dbf_path_hints"], ["/dmdata/data/DAMENG/SYSTEM.DBF"])
        self.assertEqual(len(summary["dbf_path_hint_records"]), 1)
        self.assertEqual(len(summary["dbf_path_occurrences"]), 1)
        self.assertEqual(summary["printable_string_records"], [])

    def test_summarize_control_file_dbf_hint_limit_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "dm.ctl"
            path.write_bytes(
                b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\x00"
            )

            summary = summarize_control_file(path, sample_limit=0, dbf_hint_limit=1)

        self.assertEqual(summary["dbf_path_hints"], ["/dmdata/data/DAMENG/SYSTEM.DBF"])
        self.assertEqual(len(summary["dbf_path_occurrences"]), 1)
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
