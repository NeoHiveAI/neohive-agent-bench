"""HIVE-340 — tests for the chronological timeline ordering (pure functions).

The git ancestry guard + HF pull are host-side (clone + datasets) and are exercised on the
run host; here we lock down the deterministic ordering logic. Stdlib only.
"""
from __future__ import annotations

import unittest

from build_timeline import _parse_iso_epoch, order_by_chronology, pr_number


class PrNumberTests(unittest.TestCase):
    def test_extracts_trailing_pr_number(self):
        self.assertEqual(pr_number("django__django-11239"), 11239)
        self.assertEqual(pr_number("scikit-learn__scikit-learn-25500"), 25500)

    def test_missing_number(self):
        self.assertEqual(pr_number("no-number-here-"), -1)


class ParseIsoTests(unittest.TestCase):
    def test_parses_z_suffix(self):
        self.assertIsInstance(_parse_iso_epoch("2019-04-24T18:14:03Z"), int)

    def test_monotonic(self):
        self.assertLess(_parse_iso_epoch("2019-04-24T00:00:00Z"),
                        _parse_iso_epoch("2021-01-01T00:00:00Z"))

    def test_bad_input_is_none(self):
        self.assertIsNone(_parse_iso_epoch(""))
        self.assertIsNone(_parse_iso_epoch("not-a-date"))
        self.assertIsNone(_parse_iso_epoch(None))


class OrderTests(unittest.TestCase):
    def test_orders_by_sort_ts_then_pr(self):
        rows = [
            {"instance_id": "r-300", "base_commit": "c", "sort_ts": 300},
            {"instance_id": "r-100", "base_commit": "a", "sort_ts": 100},
            {"instance_id": "r-200", "base_commit": "b", "sort_ts": 200},
        ]
        self.assertEqual([r["instance_id"] for r in order_by_chronology(rows)],
                         ["r-100", "r-200", "r-300"])

    def test_ts_ties_break_by_pr_number(self):
        rows = [
            {"instance_id": "r-1300", "base_commit": "x", "sort_ts": 500},
            {"instance_id": "r-1200", "base_commit": "y", "sort_ts": 500},
        ]
        self.assertEqual([r["instance_id"] for r in order_by_chronology(rows)],
                         ["r-1200", "r-1300"])

    def test_rows_without_ts_sort_last_by_pr(self):
        rows = [
            {"instance_id": "r-999", "base_commit": "z", "sort_ts": None},
            {"instance_id": "r-100", "base_commit": "a", "sort_ts": 100},
            {"instance_id": "r-050", "base_commit": "b", "sort_ts": None},
        ]
        # dated first (chronological), then undated by pr number
        self.assertEqual([r["instance_id"] for r in order_by_chronology(rows)],
                         ["r-100", "r-050", "r-999"])

    def test_stable_and_nondestructive(self):
        rows = [{"instance_id": "r-2", "base_commit": "b", "sort_ts": 2},
                {"instance_id": "r-1", "base_commit": "a", "sort_ts": 1}]
        order_by_chronology(rows)
        # input list order untouched (function returns a new ordering)
        self.assertEqual([r["instance_id"] for r in rows], ["r-2", "r-1"])


if __name__ == "__main__":
    unittest.main()
