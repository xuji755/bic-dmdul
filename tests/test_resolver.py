import tempfile
import unittest
from pathlib import Path

from dmdul.resolver import resolve_offline_table_metadata


PAGE_SIZE = 8192


class OfflineResolverTest(unittest.TestCase):
    def test_resolves_table_metadata_from_synthetic_database_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_dir = Path(tmp_dir)
            system = database_dir / "SYSTEM.DBF"
            user_file = database_dir / "DMDUL_TS01.DBF"
            user_file2 = database_dir / "DMDUL_TS02.DBF"
            (database_dir / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS01.DBF\0"
                b"DATAFILE=/dmdata/data/DAMENG/DMDUL_TS02.DBF\0"
            )
            _write_dbf(system, _page0(group_raw=0, kind=0x13) + _system_payload())
            user_file.write_bytes(_user_data_file_payload())
            user_file2.write_bytes(_page0(group_raw=0x00010006, kind=0x13))

            resolved = resolve_offline_table_metadata(
                database_dir=database_dir,
                table_name="DMDUL_MANY",
                scan_pages=64,
            )

        self.assertEqual(resolved.table_object_id, 33629)
        self.assertEqual(resolved.index_child.index_id, 33595349)
        self.assertEqual(resolved.table.storage.group_id, 6)
        self.assertEqual(resolved.table.storage.file_no, 0)
        self.assertEqual(resolved.table.storage.root_page, 80)
        self.assertEqual(resolved.table.storage.scan_pages, 64)
        self.assertEqual(
            [(col.name, col.type_name, col.length) for col in resolved.table.columns],
            [
                ("ID", "INT", 4),
                ("MARKER", "VARCHAR", 64),
                ("PAD", "VARCHAR", 3000),
            ],
        )
        self.assertEqual(
            sorted(item.path.name for item in resolved.metadata.data_files),
            ["DMDUL_TS01.DBF", "DMDUL_TS02.DBF"],
        )
        manifest = resolved.as_manifest()
        self.assertEqual(manifest["mode"], "dmctl-system-sysdict-segment-root")
        self.assertEqual(manifest["diagnostics"], [])
        self.assertEqual(manifest["table"], "SYSDBA.DMDUL_MANY")
        self.assertEqual(manifest["table_object"]["object_id"], 33629)
        self.assertEqual(manifest["segment"]["group_id"], 6)
        self.assertEqual(manifest["segment"]["root_file"], 0)
        self.assertEqual(manifest["segment"]["root_page"], 80)
        self.assertTrue(manifest["segment_root"]["identity_ok"])
        self.assertEqual(manifest["segment_root"]["root_header"]["page_no"], 80)
        self.assertEqual(
            manifest["segment_root"]["candidate_page_refs"][0]["page_no"],
            96,
        )
        self.assertEqual(
            sorted(Path(item["path"]).name for item in manifest["data_files"]),
            ["DMDUL_TS01.DBF", "DMDUL_TS02.DBF"],
        )
        manifest_files = {
            Path(item["path"]).name: item for item in manifest["data_files"]
        }
        self.assertEqual(
            manifest_files["DMDUL_TS01.DBF"]["control_file_entries"][0][
                "control_file_ordinal"
            ],
            1,
        )
        self.assertEqual(
            manifest_files["DMDUL_TS01.DBF"]["control_file_entries"][0]["basename"],
            "dmdul_ts01.dbf",
        )
        self.assertEqual(
            manifest_files["DMDUL_TS02.DBF"]["control_file_entries"][0][
                "control_file_ordinal"
            ],
            2,
        )
        control_files = manifest["control_file_data_files"]
        self.assertEqual(control_files["entries_total"], 3)
        self.assertEqual(
            {
                item["basename"]
                for item in control_files["matched_entries"]
            },
            {"system.dbf", "dmdul_ts01.dbf", "dmdul_ts02.dbf"},
        )

    def test_manifest_warns_when_data_file_has_no_control_file_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_dir = Path(tmp_dir)
            system = database_dir / "SYSTEM.DBF"
            user_file = database_dir / "DMDUL_TS01.DBF"
            (database_dir / "dm.ctl").write_bytes(
                b"\0DATAFILE=/dmdata/data/DAMENG/SYSTEM.DBF\0"
            )
            _write_dbf(system, _page0(group_raw=0, kind=0x13) + _system_payload())
            user_file.write_bytes(_user_data_file_payload())

            resolved = resolve_offline_table_metadata(
                database_dir=database_dir,
                table_name="DMDUL_MANY",
                scan_pages=64,
            )

        manifest = resolved.as_manifest()
        self.assertEqual(
            manifest["diagnostics"][0]["code"],
            "segment-manifest-data-file-without-control-entry",
        )
        self.assertEqual(manifest["diagnostics"][0]["level"], "warning")
        self.assertEqual(manifest["diagnostics"][0]["count"], 1)
        self.assertEqual(
            Path(manifest["diagnostics"][0]["data_files"][0]["path"]).name,
            "DMDUL_TS01.DBF",
        )
        manifest_files = {
            Path(item["path"]).name: item for item in manifest["data_files"]
        }
        self.assertEqual(manifest_files["DMDUL_TS01.DBF"]["control_file_entries"], [])


def _page0(*, group_raw: int, kind: int) -> bytes:
    page = bytearray(PAGE_SIZE)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = (0).to_bytes(4, "little")
    page[20:24] = kind.to_bytes(4, "little")
    return bytes(page)


def _write_dbf(path: Path, payload: bytes) -> None:
    path.write_bytes(payload + b"\0" * PAGE_SIZE)


def _user_data_file_payload() -> bytes:
    pages = [_page0(group_raw=6, kind=0x13)]
    pages.extend(bytes(PAGE_SIZE) for _ in range(1, 80))
    pages.append(_segment_root_page(group_raw=6, page_no=80, leaf_page=96))
    pages.extend(bytes(PAGE_SIZE) for _ in range(81, 96))
    pages.append(_page(group_raw=6, page_no=96, kind=0x14))
    return b"".join(pages)


def _segment_root_page(*, group_raw: int, page_no: int, leaf_page: int) -> bytes:
    page = bytearray(_page(group_raw=group_raw, page_no=page_no, kind=0x15))
    page[128:134] = (0).to_bytes(2, "little") + leaf_page.to_bytes(4, "little")
    return bytes(page)


def _page(*, group_raw: int, page_no: int, kind: int) -> bytes:
    page = bytearray(PAGE_SIZE)
    page[0:4] = group_raw.to_bytes(4, "little")
    page[4:8] = page_no.to_bytes(4, "little")
    page[8:14] = b"\xff" * 6
    page[14:20] = b"\xff" * 6
    page[20:24] = kind.to_bytes(4, "little")
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
    return b"".join(
        [
            _syscolumn(table_id, 0, 4, "ID", "INT"),
            _syscolumn(table_id, 1, 64, "MARKER", "VARCHAR"),
            _syscolumn(table_id, 2, 3000, "PAD", "VARCHAR"),
        ]
    )


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
