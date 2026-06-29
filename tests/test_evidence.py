import tempfile
import unittest
from pathlib import Path

from dmdul.evidence import capture_data_file_evidence, parse_page_selection


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


if __name__ == "__main__":
    unittest.main()
