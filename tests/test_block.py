from __future__ import annotations

import unittest

from dmdul.block import (
    analyze_data_block,
    dump_unknown_page_structures,
    parse_column_specs,
)
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

    def test_traces_and_decodes_fixed_temporal_types(self) -> None:
        page = bytearray(b"\0" * 8192)
        page[20:24] = (0x14).to_bytes(4, "little")
        page[0x62:0x70] = (
            bytes.fromhex("00 0e 00")
            + bytes.fromhex("ea 07 f3")
            + bytes.fromhex("ea 07 f3 6a 61 e2 f7 13")
        )

        analysis = analyze_data_block(
            page=bytes(page),
            columns=(
                ColumnMeta(name="D", type_name="DATE", length=3),
                ColumnMeta(name="TS", type_name="TIMESTAMP", length=8),
            ),
        )

        row = analysis["rows"][0]
        self.assertEqual(row["decode_status"], "ok")
        self.assertEqual(
            row["decoded_values"],
            ["2026-06-30", "2026-06-30 10:11:12.654321"],
        )
        self.assertEqual(row["field_trace"][0]["raw_hex"], "ea07f3")
        self.assertEqual(row["field_trace"][1]["raw_hex"], "ea07f36a61e2f713")

    def test_traces_fixed_area_before_variable_area(self) -> None:
        page = bytearray(b"\0" * 8192)
        page[20:24] = (0x14).to_bytes(4, "little")
        page[0x62:0x76] = (
            bytes.fromhex("00 14 00 00")
            + (7).to_bytes(4, "little", signed=True)
            + bytes.fromhex("01 02 03")
            + bytes([0x81])
            + bytes.fromhex("80")
            + bytes([0x81])
            + b"X"
            + bytes([0x82])
            + b"YZ"
        )

        analysis = analyze_data_block(
            page=bytes(page),
            columns=(
                ColumnMeta(name="ID", type_name="INT", length=4),
                ColumnMeta(name="N", type_name="NUMBER", length=22),
                ColumnMeta(name="D", type_name="DATE", length=3),
                ColumnMeta(name="C", type_name="CHAR", length=10),
                ColumnMeta(name="V", type_name="VARCHAR", length=10),
            ),
        )

        trace = analysis["rows"][0]["field_trace"]
        self.assertEqual(
            [(item["name"], item["storage_area"], item["relative_offset"]) for item in trace],
            [
                ("ID", "fixed", 4),
                ("D", "fixed", 8),
                ("N", "variable", 11),
                ("C", "variable", 13),
                ("V", "variable", 15),
            ],
        )
        self.assertEqual(trace[2]["raw_hex"], "80")
        self.assertEqual(trace[3]["text"], "X")
        self.assertEqual(trace[4]["text"], "YZ")

    def test_dumps_unknown_page_structures(self) -> None:
        page = bytearray(b"\0" * 8192)
        page[0:4] = (6).to_bytes(4, "little")
        page[4:8] = (208).to_bytes(4, "little")
        page[20:24] = (0x14).to_bytes(4, "little")
        page[0x18:0x30] = bytes.fromhex(
            "01 02 03 04 05 06 07 08"
            "09 0a 0b 0c 0d 0e 0f 10"
            "11 12 13 14 15 16 17 18"
        )
        page[0x62:0x62 + 37] = (
            bytes.fromhex("00 25 00")
            + b"A" * 15
            + bytes.fromhex("01 00 00 00 00 00 ff ff ff ff 7f ff ff 31 d7 34 04 00 00")
        )
        page[8190:8192] = (0x62).to_bytes(2, "little")

        payload = dump_unknown_page_structures(page=bytes(page), page_no=208)

        self.assertEqual(payload["mode"], "dm-unknown-page-structure-dump")
        self.assertEqual(payload["slot_row_offsets"], [0x62])
        header_region = payload["regions"][0]
        self.assertEqual(header_region["name"], "page-header-anonymous")
        self.assertEqual(header_region["runs"][0]["chunks"]["24"][0]["offset"], 0x18)
        row_tail = payload["regions"][2]
        self.assertEqual(row_tail["name"], "row-0-tail-control")
        self.assertEqual(row_tail["length"], 19)


class ColumnSpecTest(unittest.TestCase):
    def test_parse_column_specs(self) -> None:
        columns = parse_column_specs(("ID:INT:4", "NAME:VARCHAR:64", "N:NUMBER"))

        self.assertEqual(
            [(item.name, item.type_name, item.length) for item in columns],
            [("ID", "INT", 4), ("NAME", "VARCHAR", 64), ("N", "NUMBER", None)],
        )


if __name__ == "__main__":
    unittest.main()
