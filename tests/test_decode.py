from __future__ import annotations

import unittest

from dmdul.decode import DecodeError, decode_observed_row_values
from dmdul.metadata import ColumnMeta
from dmdul.row import ObservedRow, ObservedRowHeader


class ObservedRowDecodeTest(unittest.TestCase):
    def test_decode_int_and_varchar(self) -> None:
        data = (
            bytes.fromhex("00 1a 00")
            + (-7).to_bytes(4, "little", signed=True)
            + bytes.fromhex("86")
            + b"VALUE!"
            + b"\0" * 13
        )
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="ID", type_name="INT"),
                ColumnMeta(name="V", type_name="VARCHAR"),
            ),
        )

        self.assertEqual(values, [-7, "VALUE!"])

    def test_decode_bigint(self) -> None:
        data = (
            bytes.fromhex("00 13 00")
            + (9223372036854775807).to_bytes(8, "little", signed=True)
            + b"\0" * 6
        )
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        values = decode_observed_row_values(
            row,
            (ColumnMeta(name="B", type_name="BIGINT"),),
        )

        self.assertEqual(values, [9223372036854775807])

    def test_decode_double(self) -> None:
        data = bytes.fromhex("00 13 00 00 00 00 00 00 00 f8 3f") + b"\0" * 6
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        values = decode_observed_row_values(
            row,
            (ColumnMeta(name="D", type_name="DOUBLE"),),
        )

        self.assertEqual(values, [1.5])

    def test_decode_float_length_4(self) -> None:
        data = bytes.fromhex("00 0f 00 00 00 c0 3f") + b"\0" * 8
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        values = decode_observed_row_values(
            row,
            (ColumnMeta(name="F", type_name="FLOAT", length=4),),
        )

        self.assertAlmostEqual(values[0], 1.5)


    def test_decode_small_integer_aliases(self) -> None:
        data = bytes.fromhex("00 0a 00") + bytes([0x7f]) + (-1234).to_bytes(2, "little", signed=True) + (42).to_bytes(4, "little", signed=True)
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="T", type_name="TINYINT"),
                ColumnMeta(name="S", type_name="SMALLINT"),
                ColumnMeta(name="I", type_name="INTEGER"),
            ),
        )

        self.assertEqual(values, [127, -1234, 42])

    def test_decode_date_time_timestamp(self) -> None:
        date_raw = bytes.fromhex("ea 07 f3")
        time_raw = bytes.fromhex("6a 61 e2 f7 13")
        ts_raw = date_raw + time_raw
        data = bytes.fromhex("00 14 00") + date_raw + time_raw + ts_raw
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="D", type_name="DATE"),
                ColumnMeta(name="T", type_name="TIME"),
                ColumnMeta(name="TS", type_name="TIMESTAMP"),
            ),
        )

        self.assertEqual(values, ["2026-06-30", "10:11:12.654321", "2026-06-30 10:11:12.654321"])

    def test_decode_number_base100(self) -> None:
        data = bytes.fromhex("00 1b 00 81 80 82 c1 02 94 d3 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f 5b 0d 23 39 4f")
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="Z", type_name="NUMBER"),
                ColumnMeta(name="O", type_name="DECIMAL"),
                ColumnMeta(name="N", type_name="NUMBER"),
            ),
        )

        self.assertEqual(values, ["0", "1", "12345678901234567890123456789012345678"])

    def test_decode_lob_locator_as_hex_payload(self) -> None:
        data = bytes.fromhex("00 0b 00 84 ca fe ba be")
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="B", type_name="BLOB"),))

        self.assertEqual(values, ["cafebabe"])

    def test_decode_fixed_area_before_variable_area_returns_sql_order(self) -> None:
        date_raw = bytes.fromhex("ea 07 f3")
        data = (
            bytes.fromhex("00 14 00")
            + (7).to_bytes(4, "little", signed=True)
            + date_raw
            + bytes.fromhex("82 c1 02")
            + bytes([0x86])
            + b"MARKER"
        )
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="ID", type_name="INT"),
                ColumnMeta(name="N", type_name="NUMBER"),
                ColumnMeta(name="D", type_name="DATE"),
                ColumnMeta(name="V", type_name="VARCHAR"),
            ),
        )

        self.assertEqual(values, [7, "1", "2026-06-30", "MARKER"])

    def test_rejects_nonzero_row_metadata_before_column_payload(self) -> None:
        data = bytes.fromhex("00 0f 01") + (7).to_bytes(4, "little", signed=True)
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        with self.assertRaises(DecodeError) as cm:
            decode_observed_row_values(row, (ColumnMeta(name="ID", type_name="INT"),))

        self.assertEqual(cm.exception.code, "unsupported-row-metadata")


if __name__ == "__main__":
    unittest.main()
