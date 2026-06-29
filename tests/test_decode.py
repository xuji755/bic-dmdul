import unittest

from dmdul.decode import decode_observed_row_values
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


if __name__ == "__main__":
    unittest.main()
