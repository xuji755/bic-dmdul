from __future__ import annotations

import unittest

from dmdul.page import ObservedPageHeader, ObservedPageRef


class ObservedPageRefTest(unittest.TestCase):
    def test_null_ref(self) -> None:
        ref = ObservedPageRef(b"\xff" * 6)

        self.assertTrue(ref.is_null)
        self.assertIsNone(ref.file_no)
        self.assertIsNone(ref.page_no)
        self.assertEqual(str(ref), "null")

    def test_file_page_ref(self) -> None:
        ref = ObservedPageRef(bytes.fromhex("000060000000"))

        self.assertFalse(ref.is_null)
        self.assertEqual(ref.file_no, 0)
        self.assertEqual(ref.page_no, 96)


class ObservedPageHeaderTest(unittest.TestCase):
    def test_parse_header_prefix(self) -> None:
        raw = bytes.fromhex(
            "06 00 00 00 61 00 00 00 00 00 60 00 00 00 00 00"
            "62 00 00 00 14 00 00 00 ca cb c0 d5 e6 77 a5 a4"
            "05 00 00 00 04 00 20 18 00 00 00 00 02 00 ff ff"
            "52 00 5a 00 00 00 30 18 00 00 d5 9f 00 02 00 00"
        )

        header = ObservedPageHeader.from_page(raw)

        self.assertEqual(header.page_type_raw, 0x06)
        self.assertEqual(header.group_id, 6)
        self.assertEqual(header.group_raw, 6)
        self.assertEqual(header.file_no_hint, 0)
        self.assertEqual(header.page_no, 97)
        self.assertEqual(header.prev_page.file_no, 0)
        self.assertEqual(header.prev_page.page_no, 96)
        self.assertEqual(header.next_page.file_no, 0)
        self.assertEqual(header.next_page.page_no, 98)
        self.assertEqual(header.page_kind_raw, 0x14)
        self.assertEqual(header.page_kind_label, "tentative-btree-data")
        self.assertEqual(header.as_dict()["page_type_raw"], 0x06)

    def test_unknown_page_kind_label(self) -> None:
        raw = bytearray(b"\0" * 64)
        raw[20:24] = (0x12345678).to_bytes(4, "little")

        header = ObservedPageHeader.from_page(bytes(raw))

        self.assertEqual(header.page_kind_raw, 0x12345678)
        self.assertEqual(header.page_kind_label, "unknown")

    def test_group_raw_splits_file_hint_and_group_id(self) -> None:
        raw = bytearray(b"\0" * 64)
        raw[0:4] = (0x00020004).to_bytes(4, "little")

        header = ObservedPageHeader.from_page(bytes(raw))

        self.assertEqual(header.group_raw, 0x00020004)
        self.assertEqual(header.group_id, 4)
        self.assertEqual(header.file_no_hint, 2)


if __name__ == "__main__":
    unittest.main()
