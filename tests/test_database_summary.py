import tempfile
import unittest
from pathlib import Path

from dmdul.database_summary import summarize_database_dir


class DatabaseSummaryTest(unittest.TestCase):
    def test_summarizes_groups_system_candidate_and_catalog_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
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
        self.assertEqual(summary["system_candidates"], [str(root / "SYSTEM.DBF")])
        self.assertEqual(summary["warnings"], [])
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

    def test_reports_file_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad = root / "BAD.DBF"
            bad.write_bytes(
                _page(group_raw=6, page_no=9, page_kind=0x13)
                + _page(group_raw=6, page_no=99, page_kind=0x14)
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
        self.assertEqual(summary["diagnostics"]["files_with_diagnostics"], 1)
        self.assertEqual(summary["diagnostics"]["counts_by_code"]["trailing-bytes"], 1)
        self.assertEqual(
            summary["diagnostics"]["counts_by_code"]["catalog-page-number-mismatch"],
            1,
        )


def _page0(group_raw: int, page_kind: int) -> bytes:
    return _page(group_raw, 0, page_kind)


def _page(group_raw: int, page_no: int, page_kind: int) -> bytes:
    page = bytearray(128)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:20] = b"\xff" * 12
    page[20:24] = page_kind.to_bytes(4, "little")
    page[64] = 1
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
