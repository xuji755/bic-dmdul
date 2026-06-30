import unittest

from dmdul.row import (
    ObservedRow,
    ObservedRowHeader,
    decode_observed_var_length,
    describe_observed_row_layout,
    find_observed_row_slots,
    iter_observed_rows_by_slots,
    iter_observed_rows,
    scan_observed_row_chain,
)


class ObservedRowHeaderTest(unittest.TestCase):
    def test_live_row_length(self) -> None:
        header = ObservedRowHeader.from_bytes(bytes.fromhex("0025"))

        self.assertFalse(header.is_deleted)
        self.assertEqual(header.length, 0x25)

    def test_deleted_row_length(self) -> None:
        header = ObservedRowHeader.from_bytes(bytes.fromhex("8027"))

        self.assertTrue(header.is_deleted)
        self.assertEqual(header.length, 0x27)


class ObservedRowLayoutTest(unittest.TestCase):
    def test_describes_one_byte_metadata_for_up_to_four_columns(self) -> None:
        row = _observed_row(bytes.fromhex("00 0f 00 07 00 00 00"))

        layout = describe_observed_row_layout(row, column_count=4)

        self.assertEqual(layout.header_size, 2)
        self.assertEqual(layout.metadata, bytes.fromhex("00"))
        self.assertEqual(layout.metadata_size, 1)
        self.assertEqual(layout.column_payload_offset, 3)
        self.assertFalse(layout.has_unsupported_metadata)

    def test_describes_two_byte_metadata_for_five_or_more_columns(self) -> None:
        row = _observed_row(bytes.fromhex("00 10 00 00 07 00 00 00"))

        layout = describe_observed_row_layout(row, column_count=5)

        self.assertEqual(layout.metadata, bytes.fromhex("00 00"))
        self.assertEqual(layout.metadata_size, 2)
        self.assertEqual(layout.column_payload_offset, 4)

    def test_describes_five_byte_metadata_for_seventeen_columns(self) -> None:
        row = _observed_row(bytes.fromhex("00 10 00 00 00 00 00 07 00 00 00"))

        layout = describe_observed_row_layout(row, column_count=17)

        self.assertEqual(layout.metadata, bytes.fromhex("00 00 00 00 00"))
        self.assertEqual(layout.metadata_size, 5)
        self.assertEqual(layout.column_payload_offset, 7)

    def test_marks_nonzero_metadata_as_unsupported_for_now(self) -> None:
        row = _observed_row(bytes.fromhex("00 0f 01 07 00 00 00"))

        layout = describe_observed_row_layout(row, column_count=1)

        self.assertEqual(layout.metadata, bytes.fromhex("01"))
        self.assertTrue(layout.has_unsupported_metadata)

    def test_rejects_rows_shorter_than_layout_prefix(self) -> None:
        row = _observed_row(bytes.fromhex("00 02"))

        with self.assertRaises(ValueError):
            describe_observed_row_layout(row, column_count=1)


class ObservedVarLengthTest(unittest.TestCase):
    def test_short_length(self) -> None:
        decoded = decode_observed_var_length(bytes.fromhex("8a"))

        self.assertEqual(decoded.length, 10)
        self.assertEqual(decoded.encoded_size, 1)

    def test_short_max_observed_boundary(self) -> None:
        decoded = decode_observed_var_length(bytes.fromhex("ff"))

        self.assertEqual(decoded.length, 127)
        self.assertEqual(decoded.encoded_size, 1)

    def test_long_length_128(self) -> None:
        decoded = decode_observed_var_length(bytes.fromhex("0080"))

        self.assertEqual(decoded.length, 128)
        self.assertEqual(decoded.encoded_size, 2)

    def test_long_length_256(self) -> None:
        decoded = decode_observed_var_length(bytes.fromhex("0100"))

        self.assertEqual(decoded.length, 256)
        self.assertEqual(decoded.encoded_size, 2)

    def test_long_length_1000(self) -> None:
        decoded = decode_observed_var_length(bytes.fromhex("03e8"))

        self.assertEqual(decoded.length, 1000)
        self.assertEqual(decoded.encoded_size, 2)


class ObservedRowIteratorTest(unittest.TestCase):
    def test_slices_rows_by_length_and_keeps_deleted_flag(self) -> None:
        page = bytearray(b"\0" * 256)
        page[0x62:0x87] = bytes.fromhex(
            "00 25 00 01 00 00 00 8a"
        ) + b"MOD_KEEP_1" + bytes.fromhex(
            "01 00 00 00 00 00 ff ff ff ff 7f ff ff 31 d7 34 04 00 00"
        )
        page[0x87:0xAE] = bytes.fromhex("80 27") + b"\0" * (0x27 - 2)
        page[0xAE:0xDB] = bytes.fromhex(
            "00 2d 00 03 00 00 00 92"
        ) + b"MOD_UPDATE_3_AFTER" + bytes.fromhex(
            "03 00 00 00 00 00 00 01 13 00 00 97 00 32 d7 34 04 00 00 d7"
        )

        rows = iter_observed_rows(bytes(page), row_count=3)

        self.assertEqual([row.page_offset for row in rows], [0x62, 0x87, 0xAE])
        self.assertEqual([row.length for row in rows], [0x25, 0x27, 0x2D])
        self.assertEqual([row.is_deleted for row in rows], [False, True, False])

    def test_scans_physical_chain_beyond_active_row_count(self) -> None:
        page = bytearray(b"\0" * 256)
        page[0x62:0x87] = bytes.fromhex("00 25") + b"A" * (0x25 - 2)
        page[0x87:0xAE] = bytes.fromhex("80 27") + b"D" * (0x27 - 2)
        page[0xAE:0xDB] = bytes.fromhex("00 2d") + b"U" * (0x2D - 2)

        counted_rows = iter_observed_rows(bytes(page), row_count=2)
        scanned_rows = scan_observed_row_chain(bytes(page))

        self.assertEqual([row.page_offset for row in counted_rows], [0x62, 0x87])
        self.assertEqual(
            [row.page_offset for row in scanned_rows],
            [0x62, 0x87, 0xAE],
        )
        self.assertEqual([row.is_deleted for row in scanned_rows], [False, True, False])


class ObservedRowSlotTest(unittest.TestCase):
    def test_finds_tail_slots_pointing_to_row_starts(self) -> None:
        page = bytearray(b"\0" * 256)
        page[0x62:0x66] = bytes.fromhex("00 04 01 02")
        page[0x66:0x6A] = bytes.fromhex("00 04 03 04")
        page[0xF8:0xFC] = bytes.fromhex("66 00 62 00")

        rows = scan_observed_row_chain(bytes(page), max_rows=2)
        slots = find_observed_row_slots(
            bytes(page),
            row_start_offsets={row.page_offset for row in rows},
            search_start_offset=0xF0,
        )

        self.assertEqual(slots, [0x66, 0x62])

    def test_iter_rows_by_slots_returns_live_rows_in_forward_order(self) -> None:
        page = bytearray(b"\0" * 256)
        page[0x62:0x66] = bytes.fromhex("00 04 01 02")
        page[0x66:0x6A] = bytes.fromhex("80 04 03 04")
        page[0x6A:0x6E] = bytes.fromhex("00 04 05 06")
        page[0xF8:0xFC] = bytes.fromhex("6a 00 62 00")

        rows = iter_observed_rows_by_slots(bytes(page), max_rows=3)

        self.assertEqual([row.page_offset for row in rows], [0x62, 0x6A])
        self.assertEqual([row.is_deleted for row in rows], [False, False])


def _observed_row(data: bytes) -> ObservedRow:
    return ObservedRow(
        page_offset=0x62,
        data=data,
        header=ObservedRowHeader.from_bytes(data),
    )


if __name__ == "__main__":
    unittest.main()
