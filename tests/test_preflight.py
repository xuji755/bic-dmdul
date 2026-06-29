import unittest

from dmdul.preflight import evaluate_database_summary_preflight


class PreflightTest(unittest.TestCase):
    def test_marks_default_fatal_codes_not_ok(self) -> None:
        result = evaluate_database_summary_preflight(
            {
                "diagnostics": {
                    "counts_by_code": {
                        "control-file-dbf-hint-missing": 1,
                        "catalog-page-number-mismatch": 2,
                    }
                },
                "warnings": ["sample warning"],
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["fatal_codes"], [{"code": "control-file-dbf-hint-missing", "count": 1}])
        self.assertEqual(result["nonfatal_codes"], [{"code": "catalog-page-number-mismatch", "count": 2}])
        self.assertEqual(result["warnings"], ["sample warning"])

    def test_accepts_summary_without_fatal_codes(self) -> None:
        result = evaluate_database_summary_preflight(
            {
                "diagnostics": {
                    "counts_by_code": {
                        "catalog-page-number-mismatch": 1,
                    }
                }
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["fatal_codes"], [])


if __name__ == "__main__":
    unittest.main()
