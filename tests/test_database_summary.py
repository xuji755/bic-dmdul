import tempfile
import unittest
from pathlib import Path

from dmdul.database_summary import summarize_database_dir


class DatabaseSummaryTest(unittest.TestCase):
    def test_summarizes_groups_system_candidate_and_catalog_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dm.ctl").write_bytes(
                b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00"
                b"MAIN=/dmdata/data/DAMENG/MAIN01.DBF\x00"
            )
            (root / "SYSTEM.DBF").write_bytes(_page0(0, 0x13) + _page(0, 1, 0x14))
            (root / "TEMP.DBF").write_bytes(_page0(0, 0x0) + bytes(128))
            (root / "MAIN01.DBF").write_bytes(_page0(4, 0x13) + _page(4, 1, 0x11))
            (root / "MAIN02.DBF").write_bytes(
                _page0(0x00010004, 0x13) + _page(0x00010004, 1, 0x14)
            )

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=2,
                sample_limit=4,
            )

        self.assertEqual(summary["files_total"], 4)
        self.assertEqual(summary["dbf_files_total"], 4)
        self.assertEqual(summary["control_files_total"], 1)
        self.assertEqual(summary["skipped_files_total"], 0)
        self.assertEqual(summary["system_candidates"], [str(root / "SYSTEM.DBF")])
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            summary["control_files"][0]["dbf_path_hints"],
            [
                "/dmdata/data/DAMENG/SYSTEM.DBF",
                "/dmdata/data/DAMENG/MAIN01.DBF",
            ],
        )
        first_hint = summary["control_files"][0]["dbf_path_hint_records"][0]
        self.assertEqual(first_hint["text"], "/dmdata/data/DAMENG/SYSTEM.DBF")
        self.assertEqual(first_hint["offset"], 10)
        self.assertEqual(first_hint["string_offset"], 1)
        self.assertEqual(
            summary["control_files"][0]["printable_string_records"][0]["offset"],
            1,
        )
        self.assertEqual(summary["control_file_dbf_hints"]["hints_total"], 2)
        self.assertEqual(len(summary["control_file_dbf_hints"]["matched_hints"]), 2)
        self.assertEqual(summary["control_file_dbf_hints"]["unmatched_hints"], [])
        control_manifest = summary["control_file_data_files"]
        self.assertEqual(control_manifest["entries_total"], 2)
        self.assertEqual(len(control_manifest["matched_entries"]), 2)
        self.assertEqual(control_manifest["unmatched_entries"], [])
        main_entry = {
            item["basename"]: item for item in control_manifest["entries"]
        }["main01.dbf"]
        self.assertEqual(main_entry["control_file_ordinal"], 1)
        self.assertEqual(
            main_entry["normalized_path"],
            "/dmdata/data/dameng/main01.dbf",
        )
        self.assertEqual(main_entry["matched_paths"], [str(root / "MAIN01.DBF")])
        self.assertEqual(main_entry["observed_files"][0]["group_id"], 4)
        self.assertEqual(main_entry["observed_files"][0]["file_no_hint"], 0)
        self.assertEqual(
            main_entry["observed_files"][0]["page0_kind_label"],
            "tentative-file-control",
        )
        groups = {item["group_id"]: item for item in summary["groups"]}
        self.assertEqual(groups[0]["files"], 2)
        self.assertEqual(groups[4]["file_no_hints"], [0, 1])
        by_name = {Path(item["path"]).name: item for item in summary["files"]}
        self.assertTrue(by_name["SYSTEM.DBF"]["system_candidate"])
        self.assertEqual(by_name["MAIN02.DBF"]["file_no_hint"], 1)
        self.assertEqual(by_name["MAIN02.DBF"]["page0_kind_label"], "tentative-file-control")
        self.assertEqual(
            by_name["MAIN01.DBF"]["catalog_sample"]["page_kind_counts"]["0x00000011"],
            1,
        )

    def test_reports_duplicate_group_file_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "A.DBF").write_bytes(_page0(6, 0x13))
            (root / "B.DBF").write_bytes(_page0(6, 0x13))

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=0,
            )

        self.assertEqual(len(summary["duplicate_file_hints"]), 1)
        self.assertIn("SYSTEM.DBF candidate not found", summary["warnings"])
        self.assertIn("duplicate group/file_no_hint combinations found", summary["warnings"])
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["control-file-not-found"],
            1,
        )
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["duplicate-group-file-hint"],
            1,
        )

    def test_reports_file_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad = root / "BAD.DBF"
            bad.write_bytes(
                _page(group_raw=6, page_no=9, page_kind=0x13)
                + _page(
                    group_raw=6,
                    page_no=99,
                    page_kind=0x14,
                    next_ref=bytes.fromhex("000063000000"),
                )
                + b"tail"
            )

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=2,
                sample_limit=4,
            )

        self.assertIn("one or more files have diagnostics", summary["warnings"])
        diagnostics = summary["files"][0]["diagnostics"]
        codes = {item["code"] for item in diagnostics}
        self.assertIn("trailing-bytes", codes)
        self.assertIn("page0-header-page-number", codes)
        self.assertIn("catalog-page-number-mismatch", codes)
        self.assertIn("catalog-reference-out-of-range", codes)
        self.assertEqual(
            summary["files"][0]["catalog_sample"]["reference_out_of_range"][0]["ref_page_no"],
            99,
        )
        self.assertEqual(summary["diagnostics"]["files_with_diagnostics"], 1)
        self.assertEqual(summary["diagnostics"]["counts_by_code"]["trailing-bytes"], 1)
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["catalog-page-number-mismatch"],
            1,
        )
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["catalog-reference-out-of-range"],
            1,
        )

    def test_reports_short_skipped_dbf_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "SYSTEM.DBF").write_bytes(_page0(0, 0x13))
            (root / "SHORT.DBF").write_bytes(b"too short")

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=0,
            )

        self.assertEqual(summary["dbf_files_total"], 2)
        self.assertEqual(summary["files_total"], 1)
        self.assertEqual(summary["skipped_files_total"], 1)
        self.assertEqual(summary["skipped_files"][0]["code"], "short-dbf-file")
        self.assertIn("one or more DBF files were skipped", summary["warnings"])
        self.assertEqual(summary["diagnostics"]["skipped_files"], 1)
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["short-dbf-file"],
            1,
        )

    def test_reports_control_file_dbf_hints_missing_from_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dm.ctl").write_bytes(
                b"\x00DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\x00"
                b"MISSING=/dmdata/data/DAMENG/MISSING01.DBF\x00"
            )
            (root / "SYSTEM.DBF").write_bytes(_page0(0, 0x13))

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=0,
            )

        self.assertIn(
            "one or more DBF path hints from control files were not found",
            summary["warnings"],
        )
        self.assertEqual(summary["control_file_dbf_hints"]["hints_total"], 2)
        self.assertEqual(len(summary["control_file_dbf_hints"]["matched_hints"]), 1)
        self.assertEqual(
            summary["control_file_dbf_hints"]["unmatched_hints"][0]["basename"],
            "missing01.dbf",
        )
        self.assertEqual(
            summary["control_file_data_files"]["unmatched_entries"][0]["basename"],
            "missing01.dbf",
        )
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["control-file-dbf-hint-missing"],
            1,
        )
        self.assertEqual(
            summary["summary_diagnostics"][0]["code"],
            "control-file-dbf-hint-missing",
        )

    def test_control_file_manifest_preserves_duplicate_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "dm.ctl").write_bytes(
                b"\x00A=/dmdata/data/DAMENG/MAIN01.DBF\x00"
                b"B=/dmdata/data/DAMENG/MAIN01.DBF\x00"
            )
            (root / "MAIN01.DBF").write_bytes(_page0(4, 0x13))

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=0,
            )

        entries = summary["control_file_data_files"]["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual([item["control_file_ordinal"] for item in entries], [0, 1])
        self.assertEqual([item["basename"] for item in entries], ["main01.dbf"] * 2)
        self.assertEqual(summary["control_file_data_files"]["entries_total"], 2)
        self.assertEqual(len(summary["control_file_data_files"]["matched_entries"]), 2)

    def test_reports_ambiguous_control_file_dbf_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            nested = root / "copy"
            nested.mkdir()
            (root / "dm.ctl").write_bytes(
                b"\x00DATAFILE=/dmdata/data/DAMENG/MAIN01.DBF\x00"
            )
            (root / "MAIN01.DBF").write_bytes(_page0(4, 0x13))
            (nested / "MAIN01.DBF").write_bytes(_page0(0x00010004, 0x13))

            summary = summarize_database_dir(
                database_dir=root,
                page_size=128,
                catalog_pages=0,
            )

        self.assertIn(
            "one or more DBF path hints from control files matched multiple files",
            summary["warnings"],
        )
        self.assertEqual(
            summary["control_file_data_files"]["ambiguous_entries"][0]["basename"],
            "main01.dbf",
        )
        self.assertEqual(
            len(summary["control_file_data_files"]["ambiguous_entries"][0]["observed_files"]),
            2,
        )
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["control-file-dbf-hint-ambiguous"],
            1,
        )


def _page0(group_raw: int, page_kind: int) -> bytes:
    return _page(group_raw, 0, page_kind)


def _page(
    group_raw: int,
    page_no: int,
    page_kind: int,
    *,
    prev_ref: bytes | None = None,
    next_ref: bytes | None = None,
) -> bytes:
    page = bytearray(128)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = prev_ref or (b"\xff" * 6)
    page[14:20] = next_ref or (b"\xff" * 6)
    page[20:24] = page_kind.to_bytes(4, "little")
    page[64] = 1
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
