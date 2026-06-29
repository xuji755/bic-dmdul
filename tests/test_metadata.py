import unittest
from pathlib import Path

from dmdul.metadata import CalibratedMetadata


class MetadataTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
