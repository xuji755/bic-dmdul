import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
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
                ]
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = args.func(args)

        self.assertEqual(exit_code, 1)
        self.assertIn("extract-csv preflight failed", stderr.getvalue())
        self.assertIn("fatal_preflight=control-file-not-found", stderr.getvalue())
        self.assertFalse(output.exists())


def _page0() -> bytes:
    page = bytearray(128)
    page[0:4] = (0).to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = (0x13).to_bytes(4, "little")
    return bytes(page)


if __name__ == "__main__":
    unittest.main()
