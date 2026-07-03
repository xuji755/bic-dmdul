from __future__ import annotations

import unittest
import csv
import tempfile
from pathlib import Path

from dmdul.metadata import CalibratedMetadata


class MetadataTest(unittest.TestCase):
    def test_dict_dir_preserves_column_scale_and_nullable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_csv(
                root / "file.dict",
                [
                    "dict_type",
                    "ordinal",
                    "path",
                    "basename",
                    "bytes",
                    "page_size",
                    "pages",
                    "group_id",
                    "file_no",
                ],
                [
                    {
                        "dict_type": "file",
                        "ordinal": 1,
                        "path": str(root / "DMDUL_TS01.DBF"),
                        "basename": "DMDUL_TS01.DBF",
                        "bytes": 8192,
                        "page_size": 8192,
                        "pages": 1,
                        "group_id": 6,
                        "file_no": 0,
                    }
                ],
            )
            _write_csv(
                root / "tab.dict",
                [
                    "dict_type",
                    "object_kind",
                    "owner",
                    "name",
                    "qualified_name",
                    "object_id",
                    "storage_index_id",
                    "group_id",
                    "root_file",
                    "root_page",
                ],
                [
                    {
                        "dict_type": "table",
                        "object_kind": "table",
                        "owner": "SYSDBA",
                        "name": "DMDUL_NUM",
                        "qualified_name": "SYSDBA.DMDUL_NUM",
                        "object_id": 33629,
                        "storage_index_id": 33595349,
                        "group_id": 6,
                        "root_file": 0,
                        "root_page": 0,
                    }
                ],
            )
            _write_csv(
                root / "col.dict",
                [
                    "dict_type",
                    "object_id",
                    "name",
                    "type_name",
                    "length",
                    "scale",
                    "nullable",
                ],
                [
                    {
                        "dict_type": "column",
                        "object_id": 33629,
                        "name": "AMOUNT",
                        "type_name": "DECIMAL",
                        "length": 22,
                        "scale": 4,
                        "nullable": "N",
                    }
                ],
            )

            metadata = CalibratedMetadata.from_dict_dir(root)

        column = metadata.find_table("SYSDBA.DMDUL_NUM").columns[0]
        self.assertEqual(column.scale, 4)
        self.assertFalse(column.nullable)

    def test_dict_dir_maps_huge_table_to_raux_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _write_csv(
                root / "file.dict",
                [
                    "dict_type",
                    "ordinal",
                    "path",
                    "basename",
                    "bytes",
                    "page_size",
                    "pages",
                    "group_id",
                    "file_no",
                ],
                [
                    {
                        "dict_type": "file",
                        "ordinal": 1,
                        "path": str(root / "MAIN.DBF"),
                        "basename": "MAIN.DBF",
                        "bytes": 8192,
                        "page_size": 8192,
                        "pages": 1,
                        "group_id": 4,
                        "file_no": 0,
                    }
                ],
            )
            _write_csv(
                root / "tab.dict",
                [
                    "dict_type",
                    "object_kind",
                    "owner",
                    "name",
                    "qualified_name",
                    "object_id",
                    "storage_index_id",
                    "group_id",
                    "root_file",
                    "root_page",
                ],
                [
                    {
                        "dict_type": "table",
                        "object_kind": "table",
                        "owner": "SYSDBA",
                        "name": "DMDUL_HUGE_COMP_T",
                        "qualified_name": "SYSDBA.DMDUL_HUGE_COMP_T",
                        "object_id": 34171,
                        "storage_index_id": "",
                        "group_id": "",
                        "root_file": "",
                        "root_page": "",
                    },
                    {
                        "dict_type": "table",
                        "object_kind": "table",
                        "owner": "SYSDBA",
                        "name": "DMDUL_HUGE_COMP_T$RAUX",
                        "qualified_name": "SYSDBA.DMDUL_HUGE_COMP_T$RAUX",
                        "object_id": 34173,
                        "storage_index_id": 33596002,
                        "group_id": 4,
                        "root_file": 0,
                        "root_page": 949488,
                    },
                ],
            )
            _write_csv(
                root / "col.dict",
                [
                    "dict_type",
                    "object_id",
                    "name",
                    "type_name",
                    "length",
                    "scale",
                    "nullable",
                ],
                [
                    {
                        "dict_type": "column",
                        "object_id": 34171,
                        "name": "ID",
                        "type_name": "INT",
                        "length": 4,
                        "scale": 0,
                        "nullable": "Y",
                    }
                ],
            )

            metadata = CalibratedMetadata.from_dict_dir(root)

        table = metadata.find_table("SYSDBA.DMDUL_HUGE_COMP_T")
        self.assertEqual(table.name, "DMDUL_HUGE_COMP_T")
        self.assertEqual(table.columns[0].name, "ID")
        self.assertEqual(table.storage.group_id, 4)
        self.assertEqual(table.storage.file_no, 0)
        self.assertEqual(table.storage.root_page, 949488)

    def test_builds_metadata_from_segment_manifest(self) -> None:
        metadata = CalibratedMetadata.from_segment_manifest(
            {
                "table": "SYSDBA.DMDUL_ONE",
                "columns": [
                    {"name": "ID", "type_name": "INT", "length": 4},
                ],
                "segment": {
                    "group_id": 6,
                    "root_file": 0,
                    "root_page": 80,
                    "scan_pages": 64,
                },
                "data_files": [
                    {
                        "group_id": 6,
                        "file_no": 0,
                        "path": "/tmp/DMDUL_TS01.DBF",
                        "page_size": 8192,
                    }
                ],
                "segment_root": {
                    "candidate_page_refs": [
                        {
                            "file_no": 0,
                            "page_no": 96,
                            "target_page_kind_label": "tentative-btree-data",
                        }
                    ]
                },
            }
        )

        table = metadata.find_table("SYSDBA.DMDUL_ONE")
        self.assertEqual(table.storage.group_id, 6)
        self.assertEqual(table.storage.file_no, 0)
        self.assertEqual(table.storage.root_page, 80)
        self.assertEqual(table.storage.scan_pages, 64)
        self.assertEqual(table.storage.page_numbers, (80, 96))
        self.assertEqual(
            [(item.file_no, item.page_no) for item in table.storage.page_refs],
            [(0, 80), (0, 96)],
        )
        self.assertEqual(table.columns[0].name, "ID")
        self.assertEqual(metadata.data_files[0].path, Path("/tmp/DMDUL_TS01.DBF"))

    def test_segment_manifest_parses_string_nullable(self) -> None:
        metadata = CalibratedMetadata.from_segment_manifest(
            {
                "table": "SYSDBA.DMDUL_ONE",
                "columns": [
                    {"name": "ID", "type_name": "INT", "length": 4, "nullable": "N"},
                ],
                "segment": {
                    "group_id": 6,
                    "root_file": 0,
                    "root_page": 80,
                },
                "data_files": [
                    {
                        "group_id": 6,
                        "file_no": 0,
                        "path": "/tmp/DMDUL_TS01.DBF",
                        "page_size": 8192,
                    }
                ],
            }
        )

        self.assertFalse(metadata.find_table("SYSDBA.DMDUL_ONE").columns[0].nullable)

    def test_segment_manifest_skips_non_data_root_when_leaf_candidates_exist(self) -> None:
        metadata = CalibratedMetadata.from_segment_manifest(
            {
                "table": "SYSDBA.DMDUL_MANY",
                "columns": [
                    {"name": "ID", "type_name": "INT", "length": 4},
                ],
                "segment": {
                    "group_id": 6,
                    "root_file": 0,
                    "root_page": 80,
                    "scan_pages": 64,
                },
                "data_files": [
                    {
                        "group_id": 6,
                        "file_no": 0,
                        "path": "/tmp/DMDUL_TS01.DBF",
                        "page_size": 8192,
                    }
                ],
                "segment_root": {
                    "root_header": {
                        "page_kind_label": "tentative-segment-root",
                    },
                    "candidate_page_refs": [
                        {
                            "file_no": 0,
                            "page_no": 96,
                            "target_page_kind_label": "tentative-btree-data",
                        }
                    ],
                },
            }
        )

        table = metadata.find_table("SYSDBA.DMDUL_MANY")
        self.assertEqual(table.storage.page_numbers, (96,))
        self.assertEqual(
            [(item.file_no, item.page_no) for item in table.storage.page_refs],
            [(0, 96)],
        )


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
