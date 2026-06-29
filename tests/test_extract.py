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
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "PAGE1"], ["20", "PAGE2"]])

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


if __name__ == "__main__":
    unittest.main()
