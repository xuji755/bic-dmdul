import unittest

from dmdul.block import analyze_data_block, parse_column_specs
from dmdul.metadata import ColumnMeta


class DataBlockAnalysisTest(unittest.TestCase):
    def test_analyzes_page_header_object_id_and_rows(self) -> None:
        object_id = 33629
        page = bytearray(b"\0" * 8192)
        page[0:4] = (6).to_bytes(4, "little")
        page[4:8] = (96).to_bytes(4, "little")
        page[20:24] = (0x14).to_bytes(4, "little")
        page[40:44] = object_id.to_bytes(4, "little")
        page[0x62:0x71] = (
            bytes.fromhex("00 0f 00")
            + (7).to_bytes(4, "little", signed=True)
            + bytes([0x85])
            + b"HELLO"
        )

        analysis = analyze_data_block(
            page=bytes(page),
            page_no=96,
            object_id=object_id,
            columns=(
                ColumnMeta(name="ID", type_name="INT", length=4),
                ColumnMeta(name="V", type_name="VARCHAR", length=20),
            ),
        )

        self.assertEqual(analysis["page_header"]["page_kind_raw"], 0x14)
        self.assertEqual(
            analysis["page_type_candidates"][0]["name"],
            "first-byte-page-type",
        )
        self.assertEqual(analysis["object_id_candidates"][0]["offset"], 40)
        self.assertEqual(analysis["rows_total"], 1)
        row = analysis["rows"][0]
        self.assertEqual(row["layout"]["column_payload_offset"], 3)
        self.assertEqual(row["decoded_values"], [7, "HELLO"])
        self.assertEqual(
            [(item["name"], item["status"]) for item in row["field_trace"]],
            [("ID", "fixed-width-trace"), ("V", "variable-length-trace")],
        )

    def test_traces_fixed_temporal_types_without_decoding_values(self) -> None:
        page = bytearray(b"\0" * 8192)
        page[20:24] = (0x14).to_bytes(4, "little")
        page[0x62:0x70] = (
            bytes.fromhex("00 0e 00")
            + bytes.fromhex("01 02 03")
            + bytes.fromhex("04 05 06 07 08 09 0a 0b")
        )

        analysis = analyze_data_block(
            page=bytes(page),
            columns=(
                ColumnMeta(name="D", type_name="DATE", length=3),
                ColumnMeta(name="TS", type_name="TIMESTAMP", length=8),
            ),
        )

        row = analysis["rows"][0]
        self.assertEqual(row["decode_status"], "row-decode-error")
        self.assertEqual(row["field_trace"][0]["raw_hex"], "010203")
        self.assertEqual(row["field_trace"][1]["raw_hex"], "0405060708090a0b")


class ColumnSpecTest(unittest.TestCase):
    def test_parse_column_specs(self) -> None:
        columns = parse_column_specs(("ID:INT:4", "NAME:VARCHAR:64", "N:NUMBER"))

        self.assertEqual(
            [(item.name, item.type_name, item.length) for item in columns],
            [("ID", "INT", 4), ("NAME", "VARCHAR", 64), ("N", "NUMBER", None)],
        )


if __name__ == "__main__":
    unittest.main()
