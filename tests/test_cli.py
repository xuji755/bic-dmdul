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


def _page0() -> bytes:
    page = bytearray(128)
    page[0:4] = (0).to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = (0x13).to_bytes(4, "little")
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
