import csv
import json
import tempfile
import unittest
from pathlib import Path

from dmdul.extract import extract_csv_with_calibrated_metadata
from dmdul.metadata import CalibratedMetadata


class ExtractCsvScaffoldTest(unittest.TestCase):
    def test_writes_decoded_live_rows_from_root_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(b"\0" * 8192)
            page[0x62:0x7B] = (
                bytes.fromhex("00 19 00")
                + (1).to_bytes(4, "little", signed=True)
                + bytes.fromhex("85")
                + b"ALIVE"
                + b"\0" * (0x19 - 2 - 1 - 4 - 1 - 5)
            )
            page[0x7B:0x94] = bytes.fromhex("80 19") + b"D" * (0x19 - 2)
            page[0x94:0xAE] = (
                bytes.fromhex("00 1a 00")
                + (3).to_bytes(4, "little", signed=True)
                + bytes.fromhex("86")
                + b"AFTER!"
                + b"\0" * (0x1A - 2 - 1 - 4 - 1 - 6)
            )
            data_file.write_bytes(bytes(page) + (b"\0" * 8192))
            metadata_path = root / "metadata.json"
            output_path = root / "out.csv"
            metadata_path.write_text(
                json.dumps(
                    {
                        "data_files": [
                            {
                                "group_id": 6,
                                "file_no": 0,
                                "path": str(data_file),
                                "page_size": 8192,
                            }
                        ],
                        "tables": [
                            {
                                "owner": "SYSDBA",
                                "name": "DMDUL_ONE2",
                                "storage": {
                                    "group_id": 6,
                                    "file_no": 0,
                                    "root_page": 0,
                                },
                                "columns": [
                                    {
                                        "name": "ID",
                                        "type_name": "INT",
                                        "length": 4,
                                        "nullable": True,
                                    },
                                    {
                                        "name": "V",
                                        "type_name": "VARCHAR",
                                        "length": 20,
                                        "nullable": True,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            metadata = CalibratedMetadata.from_json_file(metadata_path)
            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_ONE2",
                output=output_path,
            )

            self.assertEqual(report.rows_written, 2)
            self.assertEqual(report.rows_skipped_deleted, 1)
            self.assertEqual(report.rows_skipped_decode_error, 0)
            self.assertEqual(report.decode_errors, ())
            self.assertTrue(report.ok)
            self.assertEqual(report.diagnostics, ())
            self.assertEqual(report.as_dict()["rows_written"], 2)
            self.assertTrue(report.as_dict()["ok"])
            self.assertEqual(report.scanned_pages, (0,))
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["1", "ALIVE"], ["3", "AFTER!"]])

    def test_scans_multiple_pages_from_calibrated_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [bytearray(b"\0" * 8192) for _ in range(3)]
            pages[1][0x62:0x7B] = (
                bytes.fromhex("00 19 00")
                + (10).to_bytes(4, "little", signed=True)
                + bytes.fromhex("85")
                + b"PAGE1"
                + b"\0" * (0x19 - 2 - 1 - 4 - 1 - 5)
            )
            pages[2][0x62:0x7B] = (
                bytes.fromhex("00 19 00")
                + (20).to_bytes(4, "little", signed=True)
                + bytes.fromhex("85")
                + b"PAGE2"
                + b"\0" * (0x19 - 2 - 1 - 4 - 1 - 5)
            )
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "multi.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_MULTI",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "scan_pages": 3,
                            },
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                                {"name": "V", "type_name": "VARCHAR"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_MULTI",
                output=output_path,
            )

            self.assertEqual(report.rows_written, 2)
            self.assertEqual(report.rows_skipped_deleted, 0)
            self.assertEqual(report.rows_skipped_decode_error, 0)
            self.assertEqual(report.scanned_pages, (0, 1, 2))
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "PAGE1"], ["20", "PAGE2"]])

    def test_walks_manifest_page_refs_and_leaf_next_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [_page(page_no=index, kind=0x14) for index in range(6)]
            pages[0] = _page(page_no=0, kind=0x15)
            pages[2] = _row_page(page_no=2, next_page=5, value=10, text="LEAF2")
            pages[5] = _row_page(page_no=5, next_page=None, value=50, text="LEAF5")
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "walk.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_WALK",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "scan_pages": 1,
                                "page_numbers": [0, 2],
                            },
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                                {"name": "V", "type_name": "VARCHAR"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_WALK",
                output=output_path,
            )

            self.assertEqual(report.mode, "segment-manifest-page-ref-walk")
            self.assertEqual(report.rows_written, 2)
            self.assertEqual(report.scanned_pages, (0, 2, 5))
            self.assertEqual(report.as_dict()["scanned_pages"], [0, 2, 5])
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "LEAF2"], ["50", "LEAF5"]])

    def test_reports_page_plan_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [_page(page_no=index, kind=0x14) for index in range(3)]
            pages[2][4:8] = (99).to_bytes(4, "little")
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "mismatch.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_BAD_PLAN",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "page_numbers": [0, 2],
                            },
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_BAD_PLAN",
                output=output_path,
            )

            self.assertFalse(report.ok)
            self.assertEqual(report.scanned_pages, (0,))
            self.assertEqual(report.diagnostics[0]["code"], "page-plan-identity-mismatch")
            self.assertEqual(
                report.as_dict()["diagnostics"][0]["code"],
                "page-plan-identity-mismatch",
            )

    def test_reports_cross_file_leaf_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = _row_page(page_no=0, next_page=None, value=1, text="ONE")
            page[14:20] = (1).to_bytes(2, "little") + (7).to_bytes(4, "little")
            data_file.write_bytes(bytes(page))
            output_path = root / "cross.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_CROSS",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "page_numbers": [0],
                            },
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                                {"name": "V", "type_name": "VARCHAR"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_CROSS",
                output=output_path,
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.rows_written, 1)
            self.assertEqual(report.diagnostics[0]["code"], "page-plan-cross-file-stop")

    def test_reports_decode_failures_without_writing_bad_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(b"\0" * 8192)
            page[0x62:0x6A] = bytes.fromhex("00 08 00") + b"SHORT"
            data_file.write_bytes(bytes(page))
            output_path = root / "bad.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_BAD",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                            },
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                                {"name": "V", "type_name": "VARCHAR"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_BAD",
                output=output_path,
            )

            self.assertEqual(report.rows_written, 0)
            self.assertEqual(report.rows_skipped_decode_error, 1)
            self.assertFalse(report.ok)
            self.assertEqual(report.diagnostics[0]["code"], "row-decode-error")
            self.assertEqual(report.as_dict()["diagnostics"][0]["code"], "row-decode-error")
            self.assertIn("page=0 offset=98", report.decode_errors[0])
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"]])


def _row_page(
    *,
    page_no: int,
    next_page: int | None,
    value: int,
    text: str,
) -> bytearray:
    page = _page(page_no=page_no, kind=0x14, next_page=next_page)
    encoded = text.encode("ascii")
    page[0x62 : 0x62 + 0x19] = (
        bytes.fromhex("00 19 00")
        + value.to_bytes(4, "little", signed=True)
        + bytes([0x80 + len(encoded)])
        + encoded
        + b"\0" * (0x19 - 2 - 1 - 4 - 1 - len(encoded))
    )
    return page


def _page(*, page_no: int, kind: int, next_page: int | None = None) -> bytearray:
    page = bytearray(b"\0" * 8192)
    page[0:4] = (6).to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = b"\xff" * 6
    if next_page is None:
        page[14:20] = b"\xff" * 6
    else:
        page[14:20] = (0).to_bytes(2, "little") + next_page.to_bytes(4, "little")
    page[20:24] = kind.to_bytes(4, "little")
    return page


if __name__ == "__main__":
    unittest.main()
