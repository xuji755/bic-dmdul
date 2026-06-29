import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from dmdul.cli import build_parser


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
            with redirect_stdout(stdout):
                exit_code = args.func(args)
            report = json.loads(report_output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["rows_written"], 1)
        self.assertEqual(report["diagnostics"], [])

    def test_resolve_table_writes_segment_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = root / "segment.json"
            (root / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
            )
            (root / "SYSTEM.DBF").write_bytes(
                _large_page0(group_raw=0, page_kind=0x13) + _system_payload()
            )
            (root / "DMDUL_TS01.DBF").write_bytes(
                _large_page0(group_raw=6, page_kind=0x13) + b"\0" * 8192
            )

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

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["mode"], "dmctl-system-sysdict-segment-root")
        self.assertEqual(manifest["segment"]["group_id"], 6)
        self.assertEqual(manifest["segment"]["root_file"], 0)
        self.assertEqual(manifest["segment"]["root_page"], 80)
        self.assertEqual(manifest["columns"][0]["name"], "ID")


def _page0() -> bytes:
    page = bytearray(128)
    page[0:4] = (0).to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = (0x13).to_bytes(4, "little")
    return bytes(page)


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
    return (
        table_id.to_bytes(4, "little")
        + column_id.to_bytes(2, "little")
        + length.to_bytes(4, "little")
        + b"\0" * 8
        + bytes([0x80 + len(name)])
        + name.encode("ascii")
        + bytes([0x80 + len(type_name)])
        + type_name.encode("ascii")
        + b"\0" * 12
    )


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
