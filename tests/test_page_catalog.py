import tempfile
import unittest
from pathlib import Path

from dmdul.page_catalog import catalog_data_file_pages


class PageCatalogTest(unittest.TestCase):
    def test_catalogs_page_kinds_and_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.dbf"
            pages = [
                bytes(128),
                _page(group_raw=6, header_page_no=1, kind=0x13),
                _page(
                    group_raw=6,
                    header_page_no=99,
                    kind=0x14,
                    prev_ref=bytes.fromhex("000001000000"),
                    next_ref=bytes.fromhex("000003000000"),
                ),
                _page(group_raw=0x00010006, header_page_no=3, kind=0x14),
            ]
            path.write_bytes(b"".join(pages))

            catalog = catalog_data_file_pages(
                path=path,
                page_size=128,
                sample_limit=8,
            )

        self.assertEqual(catalog["pages_total"], 4)
        self.assertEqual(catalog["zero_pages"], 1)
        self.assertEqual(catalog["nonzero_pages"], 3)
        self.assertEqual(catalog["page_kind_counts"]["zero"], 1)
        self.assertEqual(catalog["page_kind_counts"]["0x00000013"], 1)
        self.assertEqual(catalog["page_kind_counts"]["0x00000014"], 2)
        self.assertEqual(catalog["group_id_counts"]["6"], 3)
        self.assertEqual(catalog["page_no_mismatches"][0]["page_no"], 2)
        self.assertEqual(catalog["page_no_mismatches"][0]["header_page_no"], 99)
        self.assertEqual(
            catalog["page_no_mismatches"][0]["page_kind_label"],
            "tentative-btree-data",
        )
        self.assertEqual(catalog["reference_samples"][0]["page_no"], 2)
        self.assertEqual(catalog["nonzero_samples"][2]["file_no_hint"], 1)

    def test_catalog_respects_scan_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.dbf"
            pages = [
                _page(group_raw=6, header_page_no=0, kind=0x13),
                bytes(128),
                _page(group_raw=6, header_page_no=2, kind=0x14),
            ]
            path.write_bytes(b"".join(pages))

            catalog = catalog_data_file_pages(
                path=path,
                page_size=128,
                start_page=1,
                max_pages=1,
            )

        self.assertEqual(catalog["scan"]["scanned_pages"], 1)
        self.assertEqual(catalog["zero_pages"], 1)
        self.assertEqual(catalog["nonzero_pages"], 0)

    def test_catalog_reports_same_file_reference_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.dbf"
            pages = [
                _page(group_raw=0, header_page_no=0, kind=0x13),
                _page(
                    group_raw=0,
                    header_page_no=1,
                    kind=0x14,
                    next_ref=bytes.fromhex("000063000000"),
                ),
                bytes(128),
            ]
            path.write_bytes(b"".join(pages))

            catalog = catalog_data_file_pages(
                path=path,
                page_size=128,
                sample_limit=8,
            )

        self.assertEqual(len(catalog["reference_out_of_range"]), 1)
        ref = catalog["reference_out_of_range"][0]
        self.assertEqual(ref["source_page_no"], 1)
        self.assertEqual(ref["source_header_page_no"], 1)
        self.assertEqual(ref["direction"], "next")
        self.assertEqual(ref["ref_file_no"], 0)
        self.assertEqual(ref["ref_page_no"], 99)
        self.assertTrue(ref["same_file_hint"])
        self.assertEqual(ref["pages_total"], 3)


def _page(
    *,
    group_raw: int,
    header_page_no: int,
    kind: int,
    prev_ref: bytes | None = None,
    next_ref: bytes | None = None,
) -> bytes:
    page = bytearray(128)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = header_page_no.to_bytes(4, "little")
    page[8:14] = prev_ref or (b"\xff" * 6)
    page[14:20] = next_ref or (b"\xff" * 6)
    page[20:24] = kind.to_bytes(4, "little")
    page[44:46] = (7).to_bytes(2, "little")
    page[64] = 1
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
