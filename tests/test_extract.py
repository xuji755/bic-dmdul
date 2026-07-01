from __future__ import annotations

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

    def test_filters_scan_range_by_storage_id_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [bytearray(b"\0" * 8192) for _ in range(2)]
            for page_no, storage_id, value in ((0, 111, 7), (1, 222, 99)):
                page = pages[page_no]
                page[0:4] = (6).to_bytes(4, "little")
                page[4:8] = page_no.to_bytes(4, "little")
                page[8:14] = b"\xff" * 6
                page[14:20] = b"\xff" * 6
                page[20:24] = (0x14).to_bytes(4, "little")
                page[0x3A:0x3E] = storage_id.to_bytes(4, "little")
                page[0x62:0x73] = (
                    bytes.fromhex("00 11 00")
                    + value.to_bytes(4, "little", signed=True)
                    + b"\0" * (0x11 - 2 - 1 - 4)
                )
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "filtered.csv"
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
                            "name": "DMDUL_FILTER",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "scan_pages": 2,
                                "storage_id": 111,
                            },
                            "columns": [{"name": "ID", "type_name": "INT"}],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_FILTER",
                output=output_path,
            )
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))

        self.assertEqual(report.rows_written, 1)
        self.assertEqual(report.scanned_pages, (0,))
        self.assertEqual(rows, [["ID"], ["7"]])
        self.assertIn(
            "page-plan-root-leaf-chain",
            {item["code"] for item in report.diagnostics},
        )

    def test_plans_noncontiguous_leaf_pages_from_btree_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [bytearray(b"\0" * 8192) for _ in range(36)]
            storage_id = 33595349

            def init_header(page_no: int, kind: int, *, storage: int = storage_id) -> None:
                page = pages[page_no]
                page[0:4] = (6).to_bytes(4, "little")
                page[4:8] = page_no.to_bytes(4, "little")
                page[8:14] = b"\xff" * 6
                page[14:20] = b"\xff" * 6
                page[20:24] = kind.to_bytes(4, "little")
                page[0x3A:0x3E] = storage.to_bytes(4, "little")

            def page_ref(page_no: int | None) -> bytes:
                if page_no is None:
                    return b"\xff" * 6
                return (0).to_bytes(2, "little") + page_no.to_bytes(4, "little")

            def put_row(page_no: int, value: int) -> None:
                page = pages[page_no]
                page[0x62:0x71] = (
                    bytes.fromhex("00 0f 00")
                    + value.to_bytes(4, "little", signed=True)
                    + b"\0" * (0x0F - 2 - 1 - 4)
                )

            init_header(0, 0x15)
            pages[0][0x2C:0x2E] = (2).to_bytes(2, "little")
            pages[0][0x52:0x56] = (10).to_bytes(4, "little")
            for slot_offset, entry_offset, child_page, key in (
                (8178, 0x100, 20, 3),
                (8180, 0x110, 35, 5),
            ):
                pages[0][slot_offset:slot_offset + 2] = entry_offset.to_bytes(2, "little")
                pages[0][entry_offset:entry_offset + 15] = (
                    bytes.fromhex("00 0f 00")
                    + child_page.to_bytes(4, "little")
                    + b"\0\0"
                    + key.to_bytes(4, "little")
                    + b"\0\0"
                )
            for page_no, prev_page, next_page, value in (
                (10, None, 20, 10),
                (20, 10, 35, 20),
                (35, 20, None, 35),
            ):
                init_header(page_no, 0x14)
                pages[page_no][8:14] = page_ref(prev_page)
                pages[page_no][14:20] = page_ref(next_page)
                put_row(page_no, value)
            pages[15][0:4] = (6).to_bytes(4, "little")
            pages[15][4:8] = (15).to_bytes(4, "little")
            pages[15][20:24] = (0x14).to_bytes(4, "little")
            pages[15][0x3A:0x3E] = (999).to_bytes(4, "little")
            put_row(15, 999)

            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "btree.csv"
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
                            "name": "DMDUL_BTREE",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "scan_pages": 36,
                                "storage_id": storage_id,
                            },
                            "columns": [{"name": "ID", "type_name": "INT"}],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_BTREE",
                output=output_path,
            )
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))

        self.assertEqual(report.rows_written, 3)
        self.assertEqual(report.scanned_pages, (10, 20, 35))
        self.assertEqual(rows, [["ID"], ["10"], ["20"], ["35"]])
        self.assertIn(
            "page-plan-btree-root-children",
            {item["code"] for item in report.diagnostics},
        )

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
            self.assertEqual(report.scanned_pages, (2, 5))
            self.assertEqual(report.as_dict()["scanned_pages"], [2, 5])
            self.assertEqual(report.diagnostics[0]["code"], "page-plan-start-non-data")
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "LEAF2"], ["50", "LEAF5"]])

    def test_segment_manifest_page_plan_skips_non_data_root_when_leaf_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [_page(page_no=index, kind=0x14) for index in range(6)]
            pages[0] = _page(page_no=0, kind=0x15)
            pages[2] = _row_page(page_no=2, next_page=5, value=10, text="LEAF2")
            pages[5] = _row_page(page_no=5, next_page=None, value=50, text="LEAF5")
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "manifest_walk.csv"
            metadata = CalibratedMetadata.from_segment_manifest(
                {
                    "table": "SYSDBA.DMDUL_MANIFEST",
                    "columns": [
                        {"name": "ID", "type_name": "INT"},
                        {"name": "V", "type_name": "VARCHAR"},
                    ],
                    "segment": {
                        "group_id": 6,
                        "root_file": 0,
                        "root_page": 0,
                        "scan_pages": 1,
                    },
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "segment_root": {
                        "root_header": {
                            "page_kind_label": "tentative-segment-root",
                        },
                        "candidate_page_refs": [
                            {
                                "file_no": 0,
                                "page_no": 2,
                                "target_page_kind_label": "tentative-btree-data",
                            }
                        ],
                    },
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_MANIFEST",
                output=output_path,
            )

            self.assertEqual(report.rows_written, 2)
            self.assertEqual(report.scanned_pages, (2, 5))
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "LEAF2"], ["50", "LEAF5"]])

    def test_leaf_walk_stops_before_scanning_non_data_next_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            pages = [_page(page_no=index, kind=0x14) for index in range(5)]
            pages[2] = _row_page(page_no=2, next_page=4, value=10, text="LEAF2")
            pages[4] = _page(page_no=4, kind=0x13)
            data_file.write_bytes(b"".join(bytes(page) for page in pages))
            output_path = root / "walk-stop.csv"
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
                            "name": "DMDUL_WALK_STOP",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "page_numbers": [2],
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
                table_name="SYSDBA.DMDUL_WALK_STOP",
                output=output_path,
            )

            self.assertEqual(report.rows_written, 1)
            self.assertEqual(report.scanned_pages, (2,))
            self.assertEqual(report.diagnostics[0]["code"], "page-plan-non-leaf-stop")
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["10", "LEAF2"]])

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

    def test_reports_missing_cross_file_leaf_target(self) -> None:
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

            self.assertFalse(report.ok)
            self.assertEqual(report.rows_written, 1)
            self.assertEqual(report.diagnostics[0]["code"], "page-plan-file-missing")

    def test_walks_cross_file_leaf_next_chain_when_file_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file0 = root / "DMDUL_TS01.DBF"
            data_file1 = root / "DMDUL_TS02.DBF"
            page0 = _row_page(page_no=0, next_page=None, value=1, text="ONE")
            page0[14:20] = (1).to_bytes(2, "little") + (0).to_bytes(4, "little")
            data_file0.write_bytes(bytes(page0))
            page1 = _row_page(page_no=0, next_page=None, value=2, text="TWO")
            page1[0:4] = (0x00010006).to_bytes(4, "little")
            data_file1.write_bytes(bytes(page1))
            output_path = root / "cross-ok.csv"
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file0),
                            "page_size": 8192,
                        },
                        {
                            "group_id": 6,
                            "file_no": 1,
                            "path": str(data_file1),
                            "page_size": 8192,
                        },
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_CROSS_OK",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                                "page_refs": [{"file_no": 0, "page_no": 0}],
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
                table_name="SYSDBA.DMDUL_CROSS_OK",
                output=output_path,
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.rows_written, 2)
            self.assertEqual(report.scanned_pages, (0, 0))
            self.assertEqual(
                report.as_dict()["scanned_page_refs"],
                [{"file_no": 0, "page_no": 0}, {"file_no": 1, "page_no": 0}],
            )
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID", "V"], ["1", "ONE"], ["2", "TWO"]])

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

    def test_reports_unsupported_column_types_before_scanning_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(b"\0" * 8192)
            page[0x62:0x73] = (
                bytes.fromhex("00 11 00")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x11 - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            output_path = root / "unsupported.csv"
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
                            "name": "DMDUL_UNSUPPORTED",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
                            },
                            "columns": [
                                {"name": "N", "type_name": "UNKNOWN_TYPE"},
                            ],
                        }
                    ],
                }
            )

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_UNSUPPORTED",
                output=output_path,
            )

            self.assertFalse(report.ok)
            self.assertEqual(report.rows_written, 0)
            self.assertEqual(report.scanned_pages, ())
            self.assertEqual(report.diagnostics[0]["code"], "unsupported-column-type")
            self.assertEqual(
                report.diagnostics[0]["columns"],
                [{"name": "N", "type_name": "UNKNOWN_TYPE"}],
            )
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["N"]])

    def test_reports_unsupported_row_metadata_without_writing_bad_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(b"\0" * 8192)
            page[0x62:0x71] = (
                bytes.fromhex("00 0f 01")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x0F - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            output_path = root / "unsupported_row_metadata.csv"
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
                            "name": "DMDUL_ROW_METADATA",
                            "storage": {
                                "group_id": 6,
                                "file_no": 0,
                                "root_page": 0,
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
                table_name="SYSDBA.DMDUL_ROW_METADATA",
                output=output_path,
            )

            self.assertFalse(report.ok)
            self.assertEqual(report.rows_written, 0)
            self.assertEqual(report.rows_skipped_decode_error, 1)
            self.assertEqual(report.diagnostics[0]["code"], "unsupported-row-metadata")
            self.assertIn("offset=98", report.decode_errors[0])
            with output_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
        self.assertEqual(rows, [["ID"]])

    def test_preserves_initial_manifest_diagnostics_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = _row_page(page_no=0, next_page=None, value=7, text="OK")
            data_file.write_bytes(page)
            output_path = root / "out.csv"
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
                            "name": "DMDUL_DIAG",
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
                table_name="SYSDBA.DMDUL_DIAG",
                output=output_path,
                initial_diagnostics=(
                    {
                        "level": "warning",
                        "code": "segment-manifest-data-file-without-control-entry",
                        "message": "missing control evidence",
                    },
                ),
            )

        self.assertTrue(report.ok)
        self.assertEqual(report.rows_written, 1)
        self.assertEqual(
            report.diagnostics[0]["code"],
            "segment-manifest-data-file-without-control-entry",
        )


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
