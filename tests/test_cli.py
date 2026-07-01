from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from dmdul.cli import build_parser, main


def _read_dict_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


class CliTest(unittest.TestCase):
    def test_summarize_control_file_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            control = root / "dm.ctl"
            output = root / "dmctl_summary.json"
            control.write_bytes(b"\x00PATH=/dmdata/data/DAMENG/SYSTEM.DBF\x00")

            parser = build_parser()
            args = parser.parse_args(
                [
                    "summarize-control-file",
                    str(control),
                    "--output",
                    str(output),
                ]
            )
            exit_code = args.func(args)

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["path"], str(control))
        self.assertEqual(payload["dbf_path_hints"], ["/dmdata/data/DAMENG/SYSTEM.DBF"])

    def test_write_control_ctl_writes_local_file_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "control.ctl"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/original/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/original/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(_large_page0(group_raw=0, page_kind=0x13))
            (root / "DMDUL_TS01.DBF").write_bytes(_large_page0(group_raw=6, page_kind=0x13))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "write-control-ctl",
                    str(root),
                    "--output",
                    str(output),
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            payload = json.loads(stdout.getvalue())
            rows = list(csv.reader(output.read_text(encoding="utf-8").splitlines()))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["rows_total"], 2)
        self.assertEqual(
            rows,
            [
                ["0", "0", str(root / "SYSTEM.DBF")],
                ["6", "0", str(root / "DMDUL_TS01.DBF")],
            ],
        )

    def test_write_control_ctl_without_dm_ctl_uses_dbf_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "control.ctl"
            (root / "SYSTEM.DBF").write_bytes(_large_page0(group_raw=0, page_kind=0x13))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "write-control-ctl",
                    str(root),
                    "--output",
                    str(output),
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            payload = json.loads(stdout.getvalue())
            control_ctl_text = output.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(control_ctl_text, f"0,0,{root / 'SYSTEM.DBF'}\n")
        self.assertEqual(payload["diagnostics"][0]["code"], "control-ctl-without-dm-ctl")

    def test_preflight_database_writes_json_and_returns_nonzero_on_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "preflight.json"
            (root / "SYSTEM.DBF").write_bytes(_page0() + bytes(128))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "128",
                    "preflight-database",
                    str(root),
                    "--catalog-pages",
                    "0",
                    "--output",
                    str(output),
                ]
            )
            exit_code = args.func(args)

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["preflight"]["ok"])
        self.assertEqual(
            payload["preflight"]["fatal_codes"],
            [{"code": "control-file-not-found", "count": 1}],
        )

    def test_extract_csv_database_dir_runs_preflight_before_resolving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "out.csv"
            preflight_output = root / "extract_preflight.json"
            (root / "SYSTEM.DBF").write_bytes(_page0() + bytes(128))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "128",
                    "extract-csv",
                    "--database-dir",
                    str(root),
                    "--table",
                    "SYSDBA.MISSING_TABLE",
                    "--output",
                    str(output),
                    "--preflight-catalog-pages",
                    "0",
                    "--preflight-output",
                    str(preflight_output),
                ]
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = args.func(args)
            payload = json.loads(preflight_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("extract-csv preflight failed", stderr.getvalue())
        self.assertIn("fatal_preflight=control-file-not-found", stderr.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse(payload["preflight"]["ok"])
        self.assertEqual(
            payload["summary"]["diagnostics"]["counts_by_code"]["control-file-not-found"],
            1,
        )

    def test_analyze_block_writes_json_field_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(8192)
            page[0:4] = (6).to_bytes(4, "little")
            page[4:8] = (0).to_bytes(4, "little")
            page[20:24] = (0x14).to_bytes(4, "little")
            page[40:44] = (33629).to_bytes(4, "little")
            page[0x62:0x6a] = (
                bytes.fromhex("00 08 00")
                + (7).to_bytes(4, "little", signed=True)
                + bytes([0x80])
            )
            data_file.write_bytes(bytes(page))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "analyze-block",
                    str(data_file),
                    "0",
                    "--object-id",
                    "33629",
                    "--column",
                    "ID:INT:4",
                    "--column",
                    "V:VARCHAR:20",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dm-data-block-analysis")
        self.assertEqual(payload["object_id_candidates"][0]["offset"], 40)
        self.assertEqual(payload["rows"][0]["decoded_values"], [7, ""])

    def test_dump_unknown_structures_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            page = bytearray(8192)
            page[0:4] = (6).to_bytes(4, "little")
            page[4:8] = (224).to_bytes(4, "little")
            page[20:24] = (0x14).to_bytes(4, "little")
            page[0x18:0x30] = bytes(range(1, 25))
            page[0x62:0x69] = bytes.fromhex("00 07 00 01 02 03 04")
            data_file.write_bytes(bytes(page))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "dump-unknown-structures",
                    str(data_file),
                    "--pages",
                    "0",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dm-unknown-data-file-structure-dump")
        self.assertEqual(payload["page_dumps"][0]["page_header"]["page_no"], 224)
        self.assertEqual(
            payload["page_dumps"][0]["regions"][0]["runs"][0]["chunks"]["24"][0]["offset"],
            0x18,
        )

    def test_extract_csv_metadata_json_writes_report_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            metadata_file = root / "metadata.json"
            output = root / "out.csv"
            report_output = root / "report.json"
            page = bytearray(8192)
            page[0x62:0x73] = (
                bytes.fromhex("00 11 00")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x11 - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            metadata_file.write_text(
                json.dumps(
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
                                "name": "DMDUL_ONE",
                                "storage": {
                                    "group_id": 6,
                                    "file_no": 0,
                                    "root_page": 0,
                                },
                                "columns": [
                                    {"name": "ID", "type_name": "INT"},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--metadata-json",
                    str(metadata_file),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["rows_written"], 1)
        self.assertEqual(report["diagnostics"], [])

    def test_bootstrap_dicts_writes_dict_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "dicts"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "bootstrap-dicts",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--table",
                    "SYSDBA.DMDUL_MANY",
                    "--experimental-heuristic-dicts",
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(stdout.getvalue())
            file_rows = _read_dict_csv(output_dir / "file.dict")
            user_rows = _read_dict_csv(output_dir / "user.dict")
            table_rows = _read_dict_csv(output_dir / "tab.dict")
            column_rows = _read_dict_csv(output_dir / "col.dict")
            rows_by_name = {row["basename"]: row for row in file_rows}
            artifact_exists = {
                name: (output_dir / name).exists()
                for name in (
                    "bootstrap_manifest.json",
                    "control.ctl",
                    "user.dict",
                    "tab.dict",
                    "col.dict",
                )
            }

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["mode"], "dm-bootstrap-dicts")
        self.assertEqual(manifest["rows"]["control.ctl"], 2)
        self.assertEqual(manifest["rows"]["file.dict"], 2)
        self.assertEqual(manifest["rows"]["user.dict"], 1)
        self.assertEqual(manifest["rows"]["tab.dict"], 1)
        self.assertEqual(manifest["rows"]["col.dict"], 1)
        self.assertEqual(
            artifact_exists,
            {
                "bootstrap_manifest.json": True,
                "control.ctl": True,
                "user.dict": True,
                "tab.dict": True,
                "col.dict": True,
            },
        )
        self.assertTrue(rows_by_name["SYSTEM.DBF"]["system_candidate"])
        self.assertEqual(rows_by_name["DMDUL_TS01.DBF"]["group_id"], "6")
        self.assertEqual(rows_by_name["DMDUL_TS01.DBF"]["basename"], "DMDUL_TS01.DBF")
        self.assertEqual(user_rows[0]["owner"], "SYSDBA")
        self.assertEqual(table_rows[0]["qualified_name"], "SYSDBA.DMDUL_MANY")
        self.assertEqual(table_rows[0]["root_page"], "80")
        self.assertEqual(column_rows[0]["name"], "ID")
        self.assertEqual(column_rows[0]["type_name"], "INT")
        self.assertEqual(
            manifest["steps"][2]["status"],
            "heuristic-output",
        )

    def test_bootstrap_reads_defaults_from_init_dul(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "dicts"
            init_file = root / "init.dul"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())
            init_file.write_text(
                f"DATABASE_DIR={root}\n"
                f"OUTPUT_DIR={output_dir}\n"
                "PAGE_SIZE=8192\n"
                "DOWNLOAD_DICTIONARIES=YES\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["--init-file", str(init_file), "bootstrap", "--json"])
            manifest = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["database_dir"], str(root))
        self.assertEqual(manifest["rows"]["tab.dict"], 2)
        self.assertEqual(manifest["rows"]["col.dict"], 1)

    def test_bootstrap_alias_downloads_system_dictionaries_with_b_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "dicts"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "bootstrap",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "-b",
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(stdout.getvalue())
            table_rows = _read_dict_csv(output_dir / "tab.dict")
            column_rows = _read_dict_csv(output_dir / "col.dict")

        self.assertEqual(exit_code, 0)
        rows_by_kind = {row["object_kind"]: row for row in table_rows}
        self.assertEqual(manifest["rows"]["tab.dict"], 2)
        self.assertEqual(manifest["rows"]["col.dict"], 1)
        self.assertEqual(manifest["steps"][2]["status"], "system-scan-output")
        self.assertEqual(set(rows_by_kind), {"table", "index"})
        self.assertEqual(rows_by_kind["table"]["name"], "DMDUL_MANY")
        self.assertEqual(rows_by_kind["table"]["object_id"], "33629")
        self.assertEqual(rows_by_kind["table"]["storage_index_id"], "33595349")
        self.assertEqual(rows_by_kind["table"]["root_page"], "80")
        self.assertEqual(rows_by_kind["index"]["parent_object_id"], "33629")
        self.assertEqual(rows_by_kind["index"]["storage_index_id"], "33595349")
        self.assertEqual(column_rows[0]["name"], "ID")
        self.assertEqual(column_rows[0]["type_name"], "INT")

    def test_bootstrap_dicts_keeps_target_table_dicts_empty_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "dicts"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "bootstrap-dicts",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--table",
                    "SYSDBA.DMDUL_MANY",
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["rows"]["user.dict"], 0)
        self.assertEqual(manifest["rows"]["tab.dict"], 0)
        self.assertEqual(manifest["rows"]["col.dict"], 0)
        self.assertEqual(
            manifest["steps"][2]["status"],
            "blocked-by-type-decoding",
        )
        self.assertEqual(
            manifest["diagnostics"][0]["code"],
            "bootstrap-heuristic-dictionary-output-disabled",
        )


    def test_prepare_writes_init_and_filelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            init_file = root / "init.dul"
            filelist = root / "filelist.dul"
            output_dir = root / "out"
            (root / "SYSTEM.DBF").write_bytes(_large_page0(group_raw=0, page_kind=0x13))
            (root / "DMDUL_TS01.DBF").write_bytes(_large_page0(group_raw=6, page_kind=0x13))

            parser = build_parser()
            args = parser.parse_args(
                [
                    "--page-size",
                    "8192",
                    "prepare",
                    "--database-dir",
                    str(root),
                    "--init-output",
                    str(init_file),
                    "--filelist-output",
                    str(filelist),
                    "--output-dir",
                    str(output_dir),
                    "--parallel",
                    "2",
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(stdout.getvalue())
            rows = list(csv.reader(filelist.read_text(encoding="utf-8").splitlines()))
            init_text = init_file.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["files_total"], 2)
        self.assertEqual(rows, [["0", "0", str(root / "SYSTEM.DBF")], ["6", "0", str(root / "DMDUL_TS01.DBF")]])
        self.assertIn("--filelist=", init_text)
        self.assertIn("--parallel=2", init_text)

    def test_dump_data_writes_sql_header_and_pipe_delimited_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            dict_dir = root / "dicts"
            output_dir = root / "dump"
            data_file = root / "DMDUL_TS01.DBF"
            dict_dir.mkdir()
            data_file.write_bytes(_leaf_page(page_no=0, value=7, storage_id=33595349))
            _write_dict(
                dict_dir / "file.dict",
                ["dict_type", "ordinal", "path", "basename", "bytes", "page_size", "pages", "group_id", "file_no", "page_type_raw", "page0_kind_raw", "page0_kind_label", "system_candidate"],
                [{"dict_type": "file", "ordinal": 1, "path": str(data_file), "basename": data_file.name, "bytes": 8192, "page_size": 8192, "pages": 1, "group_id": 6, "file_no": 0}],
            )
            _write_dict(
                dict_dir / "tab.dict",
                ["dict_type", "object_kind", "owner", "name", "qualified_name", "object_id", "parent_object_id", "schema_id", "subtype_name", "storage_index_id", "group_id", "root_file", "root_page", "scan_pages", "source"],
                [{"dict_type": "table", "object_kind": "table", "owner": "SYSDBA", "name": "DMDUL_ONE", "qualified_name": "SYSDBA.DMDUL_ONE", "object_id": 33629, "storage_index_id": 33595349, "group_id": 6, "root_file": 0, "root_page": 0, "scan_pages": 1}],
            )
            _write_dict(
                dict_dir / "col.dict",
                ["dict_type", "owner", "table_name", "qualified_table_name", "object_id", "column_id", "ordinal", "name", "type_name", "length", "source"],
                [{"dict_type": "column", "owner": "SYSDBA", "table_name": "DMDUL_ONE", "qualified_table_name": "SYSDBA.DMDUL_ONE", "object_id": 33629, "column_id": 0, "ordinal": 1, "name": "ID", "type_name": "INT", "length": 4}],
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "dump-data",
                    "--dict-dir",
                    str(dict_dir),
                    "--output-dir",
                    str(output_dir),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--delimiter",
                    "|",
                    "--json",
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(stdout.getvalue())
            dumped = (output_dir / "SYSDBA.DMDUL_ONE.dul").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["tables_ok"], 1)
        self.assertIn("CREATE TABLE SYSDBA.DMDUL_ONE", dumped)
        self.assertIn("-- DATA", dumped)
        self.assertIn("ID\n7\n", dumped)

    def test_resolve_table_writes_segment_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "segment.json"
            csv_output = root / "out.csv"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())

            parser = build_parser()
            args = parser.parse_args(
                [
                    "resolve-table",
                    str(root),
                    "--table",
                    "SYSDBA.DMDUL_MANY",
                    "--output",
                    str(output),
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            extract_args = parser.parse_args(
                [
                    "extract-csv",
                    "--segment-json",
                    str(output),
                    "--table",
                    "SYSDBA.DMDUL_MANY",
                    "--output",
                    str(csv_output),
                ]
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                extract_exit_code = extract_args.func(extract_args)
            with csv_output.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["mode"], "dmctl-system-sysdict-segment-root")
        self.assertEqual(manifest["diagnostics"], [])
        self.assertEqual(manifest["segment"]["group_id"], 6)
        self.assertEqual(manifest["segment"]["root_file"], 0)
        self.assertEqual(manifest["segment"]["root_page"], 80)
        self.assertTrue(manifest["segment_root"]["identity_ok"])
        self.assertEqual(manifest["segment_root"]["candidate_page_refs"][0]["page_no"], 96)
        self.assertEqual(
            manifest["data_files"][0]["control_file_entries"][0]["basename"],
            "dmdul_ts01.dbf",
        )
        self.assertEqual(
            manifest["data_files"][0]["control_file_entries"][0][
                "control_file_ordinal"
            ],
            1,
        )
        self.assertEqual(manifest["columns"][0]["name"], "ID")
        self.assertEqual(extract_exit_code, 0)
        self.assertEqual(rows, [["ID"], ["7"]])

    def test_extract_csv_database_dir_preserves_resolver_manifest_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "out.csv"
            report_output = root / "report.json"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(_user_data_file_payload())

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--database-dir",
                    str(root),
                    "--skip-preflight",
                    "--table",
                    "SYSDBA.DMDUL_MANY",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["diagnostics"][0]["code"],
            "segment-manifest-data-file-without-control-entry",
        )
        self.assertEqual(report["diagnostics"][0]["level"], "warning")

    def test_extract_csv_segment_json_reports_scan_range_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            segment_file = root / "segment.json"
            output = root / "out.csv"
            report_output = root / "report.json"
            page = bytearray(8192)
            page[0x62:0x73] = (
                bytes.fromhex("00 11 00")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x11 - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            segment_file.write_text(
                json.dumps(_segment_manifest_without_page_plan(data_file)),
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--segment-json",
                    str(segment_file),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["diagnostics"][0]["level"], "warning")
        self.assertEqual(
            report["diagnostics"][0]["code"],
            "page-plan-fallback-scan-range",
        )

    def test_extract_csv_segment_json_preserves_manifest_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            segment_file = root / "segment.json"
            output = root / "out.csv"
            report_output = root / "report.json"
            page = bytearray(8192)
            page[0x62:0x73] = (
                bytes.fromhex("00 11 00")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x11 - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            manifest = _segment_manifest_without_page_plan(data_file)
            manifest["diagnostics"] = [
                {
                    "level": "warning",
                    "code": "segment-manifest-data-file-without-control-entry",
                    "message": "missing control evidence",
                }
            ]
            segment_file.write_text(json.dumps(manifest), encoding="utf-8")

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--segment-json",
                    str(segment_file),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        codes = [item["code"] for item in report["diagnostics"]]
        self.assertEqual(
            codes,
            [
                "segment-manifest-data-file-without-control-entry",
                "page-plan-fallback-scan-range",
            ],
        )
        self.assertIn(
            "diagnostic=segment-manifest-data-file-without-control-entry level=warning",
            stderr.getvalue(),
        )

    def test_extract_csv_segment_json_preserves_segment_root_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            segment_file = root / "segment.json"
            output = root / "out.csv"
            report_output = root / "report.json"
            page = bytearray(8192)
            page[0x62:0x73] = (
                bytes.fromhex("00 11 00")
                + (7).to_bytes(4, "little", signed=True)
                + b"\0" * (0x11 - 2 - 1 - 4)
            )
            data_file.write_bytes(bytes(page))
            manifest = _segment_manifest_without_page_plan(data_file)
            manifest["segment_root"] = {
                "diagnostics": [
                    {
                        "level": "warning",
                        "code": "segment-root-candidate-ref-non-data-page",
                        "message": "non-data root ref",
                    }
                ]
            }
            segment_file.write_text(json.dumps(manifest), encoding="utf-8")

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--segment-json",
                    str(segment_file),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["diagnostics"][0]["code"],
            "segment-root-candidate-ref-non-data-page",
        )
        self.assertIn(
            "diagnostic=segment-root-candidate-ref-non-data-page level=warning",
            stderr.getvalue(),
        )

    def test_extract_csv_strict_page_plan_fails_scan_range_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            data_file = root / "DMDUL_TS01.DBF"
            segment_file = root / "segment.json"
            output = root / "out.csv"
            report_output = root / "report.json"
            data_file.write_bytes(bytes(8192))
            segment_file.write_text(
                json.dumps(_segment_manifest_without_page_plan(data_file)),
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(
                [
                    "extract-csv",
                    "--segment-json",
                    str(segment_file),
                    "--table",
                    "SYSDBA.DMDUL_ONE",
                    "--output",
                    str(output),
                    "--report-output",
                    str(report_output),
                    "--strict-page-plan",
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["diagnostics"][0]["level"], "error")
        self.assertEqual(
            report["diagnostics"][0]["code"],
            "page-plan-fallback-scan-range",
        )


def _page0() -> bytes:
    page = bytearray(128)
    page[0:4] = (0).to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = (0x13).to_bytes(4, "little")
    return bytes(page)


def _segment_manifest_without_page_plan(data_file: Path) -> dict[str, object]:
    return {
        "table": "SYSDBA.DMDUL_ONE",
        "columns": [{"name": "ID", "type_name": "INT"}],
        "segment": {
            "group_id": 6,
            "root_file": 0,
            "root_page": 0,
            "scan_pages": 1,
        },
        "data_files": [
            {
                "group_id": 6,
                "file_no": 0,
                "path": str(data_file),
                "page_size": 8192,
            }
        ],
    }


def _write_dict(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _large_page0(*, group_raw: int, page_kind: int) -> bytes:
    page = bytearray(8192)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = page_kind.to_bytes(4, "little")
    return bytes(page)


def _system_payload() -> bytes:
    table_id = 33629
    index_id = 33595349
    return (
        b"\0" * 4096
        + _sysobject_table_name(table_id)
        + b"\0" * 256
        + _syscolumns(table_id)
        + b"\0" * 256
        + _sysobject_index_child(parent_id=table_id, index_id=index_id)
        + b"\0" * 256
        + _sysindex(index_id=index_id)
    )


def _user_data_file_payload() -> bytes:
    pages = [_large_page0(group_raw=6, page_kind=0x13)]
    pages.extend(bytes(8192) for _ in range(1, 80))
    pages.append(_segment_root_page(page_no=80, leaf_page=96))
    pages.extend(bytes(8192) for _ in range(81, 96))
    pages.append(_leaf_page(page_no=96, value=7, storage_id=33595349))
    pages.extend(bytes(8192) for _ in range(97, 144))
    return b"".join(pages)


def _segment_root_page(*, page_no: int, leaf_page: int) -> bytes:
    page = bytearray(_large_page(group_raw=6, page_no=page_no, page_kind=0x15))
    page[128:134] = (0).to_bytes(2, "little") + leaf_page.to_bytes(4, "little")
    return bytes(page)


def _large_page(*, group_raw: int, page_no: int, page_kind: int) -> bytes:
    page = bytearray(8192)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = b"\xff" * 6
    page[14:20] = b"\xff" * 6
    page[20:24] = page_kind.to_bytes(4, "little")
    return bytes(page)


def _leaf_page(*, page_no: int, value: int, storage_id: int) -> bytes:
    page = bytearray(_large_page(group_raw=6, page_no=page_no, page_kind=0x14))
    page[0x3A:0x3E] = storage_id.to_bytes(4, "little")
    page[0x62:0x73] = (
        bytes.fromhex("00 11 00")
        + value.to_bytes(4, "little", signed=True)
        + b"\0" * (0x11 - 2 - 1 - 4)
    )
    return bytes(page)


def _sysobject_table_name(table_id: int) -> bytes:
    return (
        table_id.to_bytes(4, "little")
        + b"\0" * 16
        + bytes([0x8A])
        + b"DMDUL_MANY"
        + bytes([0x86])
        + b"SCHOBJ"
        + bytes([0x84])
        + b"UTAB"
    )


def _syscolumns(table_id: int) -> bytes:
    return _syscolumn(table_id, 0, 4, "ID", "INT")


def _syscolumn(
    table_id: int,
    column_id: int,
    length: int,
    name: str,
    type_name: str,
) -> bytes:
    body = (
        bytes.fromhex("00000c")
        + table_id.to_bytes(4, "little")
        + column_id.to_bytes(2, "little")
        + length.to_bytes(4, "little")
        + (0).to_bytes(2, "little")
        + b"Y"
        + b"\0" * 4
        + bytes([0x80 + len(name)])
        + name.encode("ascii")
        + bytes([0x80 + len(type_name)])
        + type_name.encode("ascii")
        + bytes.fromhex("ac1500000000ffffffff7fffff30d734040000")
    )
    return (len(body) + 2).to_bytes(2, "big") + body


def _sysobject_index_child(*, parent_id: int, index_id: int) -> bytes:
    name = f"INDEX{index_id}".encode("ascii")
    return (
        index_id.to_bytes(4, "little")
        + b"\0" * 12
        + parent_id.to_bytes(4, "little")
        + b"\0" * 8
        + bytes([0x86])
        + b"TABOBJ"
        + bytes([0x80 + len(name)])
        + name
    )


def _sysindex(*, index_id: int) -> bytes:
    return (
        index_id.to_bytes(4, "little")
        + b"N"
        + (6).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (80).to_bytes(4, "little")
        + b"BT"
        + (0).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
    )


if __name__ == "__main__":
    unittest.main()
