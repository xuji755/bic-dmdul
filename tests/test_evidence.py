import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dmdul.evidence import (
    capture_data_file_evidence,
    parse_page_selection,
    verify_evidence_manifest,
)


class EvidenceCaptureTest(unittest.TestCase):
    def test_parse_page_selection(self) -> None:
        self.assertEqual(parse_page_selection("0,1,16,96-98,97"), (0, 1, 16, 96, 97, 98))

    def test_parse_page_selection_rejects_invalid_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_page_selection("9-7")

    def test_capture_page_headers_and_marker_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.dbf"
            page0 = bytearray(b"\0" * 128)
            page1 = bytearray(b"\0" * 128)
            page1[0:4] = (0x00020006).to_bytes(4, "little")
            page1[4:8] = (1).to_bytes(4, "little")
            page1[8:14] = b"\xff" * 6
            page1[14:20] = bytes.fromhex("000002000000")
            page1[20:24] = (0x14).to_bytes(4, "little")
            page1[64:78] = b"FIX_TINY_ROW_1"
            path.write_bytes(bytes(page0) + bytes(page1))

            evidence = capture_data_file_evidence(
                path=path,
                page_size=128,
                pages=(1,),
                markers=("FIX_TINY_ROW_1",),
                marker_context=4,
                label="sample",
                copy_state="clean-shutdown",
                notes=("unit fixture",),
            )

        self.assertEqual(evidence["label"], "sample")
        self.assertEqual(evidence["copy_state"], "clean-shutdown")
        self.assertEqual(evidence["notes"], ["unit fixture"])
        self.assertEqual(evidence["bytes"], 256)
        self.assertEqual(evidence["pages_total"], 2)
        self.assertEqual(evidence["captured_pages"][0]["page_no"], 1)
        self.assertFalse(evidence["captured_pages"][0]["is_all_zero"])
        header = evidence["captured_pages"][0]["observed_header"]
        self.assertEqual(header["group_id"], 6)
        self.assertEqual(header["file_no_hint"], 2)
        self.assertEqual(header["page_no"], 1)
        self.assertEqual(header["page_kind_raw"], 0x14)
        matches = evidence["markers"][0]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["page_no"], 1)
        self.assertEqual(matches[0]["page_offset"], 64)
        self.assertIn("4649585f54494e595f524f575f31", matches[0]["context_hex"])

    def test_verify_evidence_manifest_accepts_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            evidence_json = root / "system_evidence.json"
            evidence_json.write_text(
                json.dumps(
                    {
                        "file": str(data_file),
                        "page_size": 8192,
                        "captured_pages": [],
                        "markers": [],
                    }
                ),
                encoding="utf-8",
            )
            reference = root / "reference.out"
            reference.write_text("reference\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "reference_output": [reference.name],
                        "copied_files": [
                            {
                                "path": data_file.name,
                                "bytes": 3,
                                "sha256": hashlib.sha256(b"abc").hexdigest(),
                            }
                        ],
                        "evidence_json": [evidence_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["evidence_files"][0]["type"], "capture-evidence")

    def test_verify_evidence_manifest_accepts_page_catalog_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            catalog_json = root / "system_catalog.json"
            catalog_json.write_text(
                json.dumps(
                    {
                        "file": str(data_file),
                        "page_size": 8192,
                        "pages_total": 1,
                        "scan": {"scanned_pages": 1},
                        "page_kind_counts": {"zero": 1},
                        "nonzero_samples": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "reference_output": ["reference.out"],
                        "copied_files": [
                            {
                                "path": data_file.name,
                                "bytes": 3,
                                "sha256": hashlib.sha256(b"abc").hexdigest(),
                            }
                        ],
                        "evidence_json": [catalog_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence_files"][0]["type"], "catalog-pages")

    def test_verify_evidence_manifest_accepts_database_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            summary_json = root / "database_summary.json"
            summary_json.write_text(
                json.dumps(
                    {
                        "database_dir": str(root),
                        "page_size": 8192,
                        "files_total": 1,
                        "groups": [],
                        "system_candidates": [str(data_file)],
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "reference_output": ["reference.out"],
                        "copied_files": [
                            {
                                "path": data_file.name,
                                "bytes": 3,
                                "sha256": hashlib.sha256(b"abc").hexdigest(),
                            }
                        ],
                        "evidence_json": [summary_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence_files"][0]["type"], "summarize-database")

    def test_verify_evidence_manifest_accepts_control_file_comparison_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            control_file = root / "dm.ctl"
            control_file.write_bytes(b"abc")
            comparison_json = root / "ctl_compare.json"
            comparison_json.write_text(
                json.dumps(
                    {
                        "before": {"path": "before.ctl", "bytes": 3, "sha256": "x"},
                        "after": {"path": "after.ctl", "bytes": 3, "sha256": "y"},
                        "same_size": True,
                        "changed_bytes": 1,
                        "changed_ranges_total": 1,
                        "changed_ranges": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "reference_output": ["reference.out"],
                        "copied_files": [
                            {
                                "path": control_file.name,
                                "bytes": 3,
                                "sha256": hashlib.sha256(b"abc").hexdigest(),
                            }
                        ],
                        "evidence_json": [comparison_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence_files"][0]["type"], "compare-control-files")

    def test_verify_evidence_manifest_rejects_unknown_evidence_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            evidence_json = root / "unknown.json"
            evidence_json.write_text(json.dumps({"file": str(data_file)}), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "copied_files": [{"path": data_file.name}],
                        "evidence_json": [evidence_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("not a recognized" in item for item in result["errors"]))

    def test_verify_evidence_manifest_reports_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "unknown",
                        "copied_files": [
                            {
                                "path": data_file.name,
                                "bytes": 99,
                                "sha256": "0" * 64,
                            }
                        ],
                        "evidence_json": ["missing.json"],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("bytes mismatch" in item for item in result["errors"]))
        self.assertTrue(any("sha256 mismatch" in item for item in result["errors"]))
        self.assertTrue(any("does not exist" in item for item in result["errors"]))
        self.assertTrue(any("copy_state is unknown" in item for item in result["warnings"]))

    def test_verify_evidence_manifest_rejects_non_numeric_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "SYSTEM.DBF"
            data_file.write_bytes(b"abc")
            evidence_json = root / "system_evidence.json"
            evidence_json.write_text(
                json.dumps(
                    {
                        "file": str(data_file),
                        "page_size": 8192,
                        "captured_pages": [],
                        "markers": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "label": "unit",
                        "copy_state": "clean-shutdown",
                        "copied_files": [
                            {
                                "path": data_file.name,
                                "bytes": "not-a-number",
                            }
                        ],
                        "evidence_json": [evidence_json.name],
                    }
                ),
                encoding="utf-8",
            )

            result = verify_evidence_manifest(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("bytes must be an integer" in item for item in result["errors"])
        )


if __name__ == "__main__":
    unittest.main()
