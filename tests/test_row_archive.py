from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dmdul.extract import extract_csv_with_calibrated_metadata
from dmdul.metadata import CalibratedMetadata
from dmdul.row_archive import import_data_to_sql, import_dul_text_to_sql, import_row_archive_to_sql


class RowArchiveTest(unittest.TestCase):
    def _page_ref(self, page_no: int | None) -> bytes:
        if page_no is None:
            return b"\xff" * 6
        return (0).to_bytes(2, "little") + page_no.to_bytes(4, "little")

    def _var_payload(self, payload: bytes) -> bytes:
        self.assertLess(len(payload), 128)
        return bytes([0x80 | len(payload)]) + payload

    def _lob_locator(self, *, lob_id: int, byte_length: int, start_page: int) -> bytes:
        return (
            b"\x02"
            + lob_id.to_bytes(4, "little")
            + b"\0" * 4
            + byte_length.to_bytes(4, "little")
            + (6).to_bytes(4, "little")
            + start_page.to_bytes(4, "little")
        )

    def _data_page(self, row_payload: bytes) -> bytes:
        page = bytearray(b"\0" * 8192)
        row = (len(row_payload) + 2).to_bytes(2, "big") + row_payload
        page[0x62 : 0x62 + len(row)] = row
        return bytes(page)

    def _lob_page(self, *, page_no: int, lob_id: int, payload: bytes) -> bytes:
        page = bytearray(b"\0" * 8192)
        page[0:4] = (6).to_bytes(4, "little")
        page[4:8] = page_no.to_bytes(4, "little")
        page[8:14] = self._page_ref(None)
        page[14:20] = self._page_ref(None)
        page[20:24] = (0x20).to_bytes(4, "little")
        page[0x24:0x28] = lob_id.to_bytes(4, "little")
        page[0x2C:0x2E] = len(payload).to_bytes(2, "little")
        page[0x38 : 0x38 + len(payload)] = payload
        return bytes(page)

    def test_row_archive_embeds_create_table_row_bytes_and_lob_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            lob_id = 0x12345678
            lob_payload = "hello一".encode("gb18030")
            locator = self._lob_locator(
                lob_id=lob_id,
                byte_length=len(lob_payload),
                start_page=1,
            )
            row_payload = (
                b"\0"
                + (7).to_bytes(4, "little", signed=True)
                + self._var_payload(locator)
            )
            data_file.write_bytes(
                self._data_page(row_payload)
                + self._lob_page(page_no=1, lob_id=lob_id, payload=lob_payload)
            )
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_ROW_ARCH",
                            "storage": {"group_id": 6, "file_no": 0, "root_page": 0},
                            "columns": [
                                {"name": "ID", "type_name": "INT"},
                                {"name": "DOC", "type_name": "CLOB"},
                            ],
                        }
                    ],
                }
            )
            archive_path = root / "SYSDBA.DMDUL_ROW_ARCH.row"

            report = extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_ROW_ARCH",
                output=archive_path,
                output_format="row",
            )
            sql_path = root / "import.sql"
            import_report = import_row_archive_to_sql(
                input_path=archive_path,
                output_sql=sql_path,
            )
            sql = sql_path.read_text(encoding="utf-8")

        self.assertTrue(report.ok)
        self.assertEqual(report.rows_written, 1)
        self.assertEqual(import_report.rows, 1)
        self.assertIn("CREATE TABLE SYSDBA.DMDUL_ROW_ARCH", sql)
        self.assertIn("INSERT INTO SYSDBA.DMDUL_ROW_ARCH (ID, DOC) VALUES (7, 'hello一');", sql)

    def test_import_data_auto_detects_row_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            row_payload = b"\0" + (7).to_bytes(4, "little", signed=True)
            data_file.write_bytes(self._data_page(row_payload))
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_ROW_ARCH",
                            "storage": {"group_id": 6, "file_no": 0, "root_page": 0},
                            "columns": [{"name": "ID", "type_name": "INT"}],
                        }
                    ],
                }
            )
            archive_path = root / "SYSDBA.DMDUL_ROW_ARCH.row"
            extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_ROW_ARCH",
                output=archive_path,
                output_format="row",
            )
            sql_path = root / "import.sql"

            report = import_data_to_sql(input_path=archive_path, output_sql=sql_path)
            sql = sql_path.read_text(encoding="utf-8")

        self.assertEqual(report.input_format, "row")
        self.assertEqual(report.rows, 1)
        self.assertIn("INSERT INTO SYSDBA.DMDUL_ROW_ARCH (ID) VALUES (7);", sql)

    def test_import_row_archive_rewrites_create_table_for_target_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            data_file.write_bytes(self._data_page(b"\0" + (7).to_bytes(4, "little", signed=True)))
            metadata = CalibratedMetadata.from_dict(
                {
                    "data_files": [
                        {
                            "group_id": 6,
                            "file_no": 0,
                            "path": str(data_file),
                            "page_size": 8192,
                        }
                    ],
                    "tables": [
                        {
                            "owner": "SYSDBA",
                            "name": "DMDUL_ROW_ARCH",
                            "storage": {"group_id": 6, "file_no": 0, "root_page": 0},
                            "columns": [{"name": "ID", "type_name": "INT"}],
                        }
                    ],
                }
            )
            archive_path = root / "SYSDBA.DMDUL_ROW_ARCH.row"
            extract_csv_with_calibrated_metadata(
                metadata=metadata,
                table_name="SYSDBA.DMDUL_ROW_ARCH",
                output=archive_path,
                output_format="row",
            )
            sql_path = root / "import.sql"

            import_data_to_sql(
                input_path=archive_path,
                output_sql=sql_path,
                table_name="DMTEST.DMDUL_ROW_ARCH",
            )
            sql = sql_path.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE DMTEST.DMDUL_ROW_ARCH", sql)
        self.assertNotIn("CREATE TABLE SYSDBA.DMDUL_ROW_ARCH", sql)
        self.assertIn("INSERT INTO DMTEST.DMDUL_ROW_ARCH (ID) VALUES (7);", sql)

    def test_import_dul_text_uses_create_table_header_and_lob_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            lob_dir = root / "SYSDBA.DMDUL_LOB.lob" / "00000001"
            lob_dir.mkdir(parents=True)
            (lob_dir / "DOC.clob").write_text("CLOB_一", encoding="utf-8")
            (lob_dir / "BIN.blob").write_bytes(bytes.fromhex("ca fe ba be"))
            dul_path = root / "SYSDBA.DMDUL_LOB.dul"
            dul_path.write_text(
                "CREATE TABLE SYSDBA.DMDUL_LOB (\n"
                "  ID INT,\n"
                "  DOC CLOB,\n"
                "  BIN BLOB\n"
                ");\n"
                "-- DATA\n"
                "ID|DOC|BIN\n"
                "7|@LOB:SYSDBA.DMDUL_LOB.lob/00000001/DOC.clob|"
                "@LOB:SYSDBA.DMDUL_LOB.lob/00000001/BIN.blob\n",
                encoding="utf-8",
            )
            sql_path = root / "import.sql"

            report = import_dul_text_to_sql(
                input_path=dul_path,
                output_sql=sql_path,
                delimiter="|",
            )
            sql = sql_path.read_text(encoding="utf-8")

        self.assertEqual(report.input_format, "dul")
        self.assertEqual(report.rows, 1)
        self.assertIn("CREATE TABLE SYSDBA.DMDUL_LOB", sql)
        self.assertIn(
            "INSERT INTO SYSDBA.DMDUL_LOB (ID, DOC, BIN) VALUES "
            "(7, 'CLOB_一', HEXTORAW('cafebabe'));",
            sql,
        )

    def test_import_dul_text_rewrites_create_table_for_target_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dul_path = root / "SYSDBA.DMDUL_ONE.dul"
            dul_path.write_text(
                "CREATE TABLE SYSDBA.DMDUL_ONE (\n"
                "  ID INT\n"
                ");\n"
                "-- DATA\n"
                "ID\n"
                "7\n",
                encoding="utf-8",
            )
            sql_path = root / "import.sql"

            import_data_to_sql(
                input_path=dul_path,
                output_sql=sql_path,
                table_name="DMTEST.DMDUL_ONE",
            )
            sql = sql_path.read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE DMTEST.DMDUL_ONE", sql)
        self.assertNotIn("CREATE TABLE SYSDBA.DMDUL_ONE", sql)
        self.assertIn("INSERT INTO DMTEST.DMDUL_ONE (ID) VALUES (7);", sql)

    def test_import_dul_text_chunks_long_string_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            long_value = "A" * 2505
            dul_path = root / "SYSDBA.DMDUL_LONG.dul"
            dul_path.write_text(
                "CREATE TABLE SYSDBA.DMDUL_LONG (\n"
                "  PAD VARCHAR(3000)\n"
                ");\n"
                "-- DATA\n"
                "PAD\n"
                f"{long_value}\n",
                encoding="utf-8",
            )
            sql_path = root / "import.sql"

            import_data_to_sql(input_path=dul_path, output_sql=sql_path)
            sql = sql_path.read_text(encoding="utf-8")

        self.assertIn("DECLARE\n  V_C1 VARCHAR(32767);\nBEGIN\n", sql)
        self.assertIn("  V_C1 := '{}';\n".format("A" * 500), sql)
        self.assertIn("  V_C1 := V_C1 || '{}';\n".format("A" * 5), sql)
        self.assertIn("INSERT INTO SYSDBA.DMDUL_LONG (PAD) VALUES (V_C1);", sql)
        self.assertNotIn("'" + long_value + "'", sql)

    def test_import_dul_text_chunks_long_blob_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            hex_value = "cafebabe" * 400
            dul_path = root / "SYSDBA.DMDUL_BLOB.dul"
            dul_path.write_text(
                "CREATE TABLE SYSDBA.DMDUL_BLOB (\n"
                "  BIN BLOB\n"
                ");\n"
                "-- DATA\n"
                "BIN\n"
                f"{hex_value}\n",
                encoding="utf-8",
            )
            sql_path = root / "import.sql"

            import_data_to_sql(input_path=dul_path, output_sql=sql_path)
            sql = sql_path.read_text(encoding="utf-8")

        self.assertIn("DECLARE\n  V_C1 BLOB;\nBEGIN\n", sql)
        self.assertIn("DBMS_LOB.CREATETEMPORARY(V_C1, TRUE);", sql)
        self.assertIn("DBMS_LOB.WRITEAPPEND(V_C1, 500, HEXTORAW('", sql)
        self.assertIn("INSERT INTO SYSDBA.DMDUL_BLOB (BIN) VALUES (V_C1);", sql)
        self.assertIn("DBMS_LOB.FREETEMPORARY(V_C1);", sql)
