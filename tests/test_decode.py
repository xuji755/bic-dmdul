from __future__ import annotations

import unittest

from dmdul.decode import DecodeError, LobValue, decode_observed_row_values
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

    def test_decode_int_and_varchar2(self) -> None:
        data = (
            bytes.fromhex("00 1a 00")
            + (8).to_bytes(4, "little", signed=True)
            + bytes.fromhex("86")
            + b"VALUE2"
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
                ColumnMeta(name="V", type_name="VARCHAR2"),
            ),
        )

        self.assertEqual(values, [8, "VALUE2"])

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

    def test_decode_dec_alias(self) -> None:
        data = bytes.fromhex("00 06 00 82 c1 2b")
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="D", type_name="DEC"),))

        self.assertEqual(values, ["42"])

    def test_decode_binary_types_as_hex(self) -> None:
        data = bytes.fromhex("00 10 00 7f 84 ca fe ba be")
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="B1", type_name="BYTE"),
                ColumnMeta(name="B2", type_name="VARBINARY"),
            ),
        )

        self.assertEqual(values, ["7f", "cafebabe"])

    def test_decode_interval_day_to_second(self) -> None:
        interval_raw = (
            (1).to_bytes(4, "little", signed=True)
            + (2).to_bytes(4, "little", signed=True)
            + (3).to_bytes(4, "little", signed=True)
            + (4).to_bytes(4, "little", signed=True)
            + (500000).to_bytes(4, "little", signed=True)
            + (1590).to_bytes(4, "little", signed=True)
        )
        data = bytes.fromhex("00 1b 00") + interval_raw
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (ColumnMeta(name="I", type_name="INTERVAL DAY TO SECOND"),),
        )

        self.assertEqual(values, ["1 02:03:04.500000"])

    def test_decode_rowid_text_mapping(self) -> None:
        data = bytes.fromhex("00 0f 00 00 00 00 00 00 00 00 00 00 00 00 01")
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="R", type_name="ROWID"),))

        self.assertEqual(values, ["AAAAAAAAAAAAAAAAAB"])

    def test_decode_text_as_utf8(self) -> None:
        data = bytes.fromhex("00 09 00 85") + b"hello"
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="T", type_name="TEXT"),))

        self.assertEqual(values, ["hello"])

    def test_decode_text_external_lob_preserves_plain_inline_text(self) -> None:
        data = bytes.fromhex("00 09 00 85") + b"hello"
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (ColumnMeta(name="T", type_name="TEXT"),),
            external_lobs=True,
        )

        self.assertIsInstance(values[0], LobValue)
        self.assertTrue(values[0].is_inline)
        self.assertEqual(values[0].inline_payload, b"hello")
        self.assertEqual(values[0].text, "hello")

    def test_decode_inline_text_lob_payload_with_gb18030_fallback(self) -> None:
        inline_text = "TEXT_VALUE_一".encode("gb18030")
        lob_payload = (
            bytes.fromhex("01 5d d4 06 00 00 00 00 00")
            + len(inline_text).to_bytes(4, "little")
            + inline_text
        )
        data = bytes.fromhex("00 1e 00") + bytes([0x80 | len(lob_payload)]) + lob_payload
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="T", type_name="TEXT"),))

        self.assertEqual(values, ["TEXT_VALUE_一"])

    def test_decode_inline_clob_payload_as_text(self) -> None:
        inline_text = b"CLOB_A_0123456789"
        lob_payload = (
            bytes.fromhex("01 33 a6 06 00 00 00 00 00")
            + len(inline_text).to_bytes(4, "little")
            + inline_text
        )
        data = bytes.fromhex("00 23 00") + bytes([0x80 | len(lob_payload)]) + lob_payload
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="C", type_name="CLOB"),))

        self.assertEqual(values, ["CLOB_A_0123456789"])

    def test_decode_lob_locator_as_hex_payload(self) -> None:
        lob_payload = bytes.fromhex("01 34 a6 06 00 00 00 00 00 04 00 00 00 ca fe ba be")
        data = bytes.fromhex("00 18 00") + bytes([0x80 | len(lob_payload)]) + lob_payload
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(row, (ColumnMeta(name="B", type_name="BLOB"),))

        self.assertEqual(values, ["cafebabe"])

    def test_decode_timezone_temporal_types(self) -> None:
        date_raw = bytes.fromhex("ea 87 0b")
        time_raw = bytes.fromhex("cd 79 e2 f7 13")
        tz_raw = bytes.fromhex("e0 01")
        data = bytes.fromhex("00 21 00") + time_raw + tz_raw + date_raw + time_raw + date_raw + time_raw + tz_raw
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="T", type_name="TIME WITH TIME ZONE"),
                ColumnMeta(name="L", type_name="TIMESTAMP WITH LOCAL TIME ZONE"),
                ColumnMeta(name="Z", type_name="DATETIME WITH TIME ZONE"),
            ),
        )

        self.assertEqual(
            values,
            [
                "13:14:15.654321 +08:00",
                "2026-07-01 13:14:15.654321",
                "2026-07-01 13:14:15.654321 +08:00",
            ],
        )

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

    def test_decode_short_char_in_fixed_area_before_decimal_variables(self) -> None:
        data = bytes.fromhex(
            "007e00000003000000030000005f870000484686c44c5831234983c00e55"
            "89735837645a4d584254934967526d375248595967757a584261575a306f"
            "9147576e4a506542644238375835677837659250454a34586b6451316d57"
            "43696e75306c378937343037313131313118000000000000d52e000037"
            "006b08ef030000"
        )
        row = ObservedRow(page_offset=0x10B1, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="D_W_ID", type_name="INTEGER", length=4, nullable=False),
                ColumnMeta(name="D_ID", type_name="INTEGER", length=4, nullable=False),
                ColumnMeta(name="D_YTD", type_name="DECIMAL", length=12, scale=2),
                ColumnMeta(name="D_TAX", type_name="DECIMAL", length=4, scale=4),
                ColumnMeta(name="D_NEXT_O_ID", type_name="INTEGER", length=4),
                ColumnMeta(name="D_NAME", type_name="VARCHAR", length=10),
                ColumnMeta(name="D_STREET_1", type_name="VARCHAR", length=20),
                ColumnMeta(name="D_STREET_2", type_name="VARCHAR", length=20),
                ColumnMeta(name="D_CITY", type_name="VARCHAR", length=20),
                ColumnMeta(name="D_STATE", type_name="CHAR", length=2),
                ColumnMeta(name="D_ZIP", type_name="CHAR", length=9),
            ),
        )

        self.assertEqual(
            values,
            [
                3,
                3,
                "75874834.72",
                "0.1384",
                34655,
                "sX7dZMXBT",
                "IgRm7RHYYguzXBaWZ0o",
                "GWnJPeBdB87X5gx7e",
                "PEJ4XkdQ1mWCinu0l7",
                "HF",
                "740711111",
            ],
        )

    def test_decode_null_bitmap_in_storage_column_order(self) -> None:
        data = (
            bytes.fromhex("00 2f 3c 00")
            + (3).to_bytes(4, "little", signed=True)
            + (20).to_bytes(4, "little", signed=True)
            + (2000).to_bytes(8, "little", signed=True)
            + bytes([0x83])
            + b"N3B"
            + bytes([0x83])
            + b"N3D"
            + b"\0" * 19
        )
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="ID", type_name="INT"),
                ColumnMeta(name="A", type_name="INT"),
                ColumnMeta(name="B", type_name="VARCHAR"),
                ColumnMeta(name="C", type_name="BIGINT"),
                ColumnMeta(name="D", type_name="VARCHAR"),
            ),
        )

        self.assertEqual(values, [3, None, "N3B", None, "N3D"])

    def test_decode_variable_nulls_without_consuming_payload(self) -> None:
        data = (
            bytes.fromhex("00 27 c0 03")
            + (2).to_bytes(4, "little", signed=True)
            + (20).to_bytes(4, "little", signed=True)
            + (2000).to_bytes(8, "little", signed=True)
            + b"\0" * 19
        )
        row = ObservedRow(page_offset=0x62, data=data, header=ObservedRowHeader.from_bytes(data))

        values = decode_observed_row_values(
            row,
            (
                ColumnMeta(name="ID", type_name="INT"),
                ColumnMeta(name="A", type_name="INT"),
                ColumnMeta(name="B", type_name="VARCHAR"),
                ColumnMeta(name="C", type_name="BIGINT"),
                ColumnMeta(name="D", type_name="VARCHAR"),
            ),
        )

        self.assertEqual(values, [2, 20, None, 2000, None])

    def test_rejects_unsupported_row_metadata_state_before_column_payload(self) -> None:
        data = bytes.fromhex("00 0f 01") + (7).to_bytes(4, "little", signed=True)
        row = ObservedRow(
            page_offset=0x62,
            data=data,
            header=ObservedRowHeader.from_bytes(data),
        )

        with self.assertRaises(DecodeError) as cm:
            decode_observed_row_values(row, (ColumnMeta(name="ID", type_name="INT"),))

        self.assertEqual(cm.exception.code, "unsupported-row-metadata")

    def test_decode_out_of_line_varchar_locator_state(self) -> None:
        locator = (
            b"\x02"
            + (0x12345678).to_bytes(4, "little")
            + b"\0" * 4
            + (3500).to_bytes(4, "little")
            + (6).to_bytes(4, "little")
            + (12054).to_bytes(4, "little")
        )
        data = (
            bytes.fromhex("00 1c 04")
            + (7).to_bytes(4, "little", signed=True)
            + bytes([0x80 + len(locator)])
            + locator
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
            external_lobs=True,
        )

        self.assertEqual(values[0], 7)
        self.assertIsInstance(values[1], LobValue)
        assert isinstance(values[1], LobValue)
        self.assertEqual(values[1].type_name, "VARCHAR")
        self.assertEqual(values[1].raw, locator)


if __name__ == "__main__":
    unittest.main()
