import tempfile
import unittest
from pathlib import Path

from dmdul.segment import analyze_segment_root


class SegmentRootTest(unittest.TestCase):
    def test_analyzes_root_identity_and_candidate_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "DMDUL_TS01.DBF"
            path.write_bytes(_dbf_with_root_and_leaf())

            segment = analyze_segment_root(
                path=path,
                page_size=128,
                group_id=6,
                file_no=0,
                root_page=4,
                known_file_nos={0},
                sample_limit=8,
            )

        self.assertTrue(segment["identity_ok"])
        self.assertEqual(segment["root_header"]["page_kind_label"], "tentative-segment-root")
        self.assertEqual(segment["diagnostics"], [])
        self.assertEqual(segment["candidate_page_refs_total"], 1)
        ref = segment["candidate_page_refs"][0]
        self.assertEqual(ref["offset"], 80)
        self.assertEqual(ref["file_no"], 0)
        self.assertEqual(ref["page_no"], 6)
        self.assertEqual(ref["target_page_kind_label"], "tentative-btree-data")

    def test_reports_out_of_range_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "DMDUL_TS01.DBF"
            path.write_bytes(_page(group_raw=6, page_no=0, kind=0x13))

            segment = analyze_segment_root(
                path=path,
                page_size=128,
                group_id=6,
                file_no=0,
                root_page=4,
            )

        self.assertFalse(segment["identity_ok"])
        self.assertEqual(
            segment["diagnostics"][0]["code"],
            "segment-root-out-of-range",
        )


def _dbf_with_root_and_leaf() -> bytes:
    pages = [
        _page(group_raw=6, page_no=0, kind=0x13),
        bytes(128),
        bytes(128),
        bytes(128),
        _root_page(),
        bytes(128),
        _page(group_raw=6, page_no=6, kind=0x14),
    ]
    return b"".join(pages)


def _root_page() -> bytes:
    page = bytearray(_page(group_raw=6, page_no=4, kind=0x15))
    page[80:86] = (0).to_bytes(2, "little") + (6).to_bytes(4, "little")
    return bytes(page)


def _page(*, group_raw: int, page_no: int, kind: int) -> bytes:
    page = bytearray(128)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = b"\xff" * 6
    page[14:20] = b"\xff" * 6
    page[20:24] = kind.to_bytes(4, "little")
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
