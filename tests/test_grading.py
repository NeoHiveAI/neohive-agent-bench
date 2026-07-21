"""HIVE-343 — tests for grading discipline (apply-failures bucketed apart).

The key property: a patch that fails to apply is NEVER counted as unresolved, and the
resolve-rate denominator excludes it. Stdlib only.
"""
from __future__ import annotations

import unittest

from grading import (
    APPLY_FAILED,
    EMPTY_PATCH,
    ERROR,
    RESOLVED,
    UNRESOLVED,
    InstanceVerdict,
    classify_from_reports,
    classify_instance,
    summarize,
)


class ClassifyTests(unittest.TestCase):
    def test_resolved(self):
        self.assertEqual(classify_instance(patch_is_empty=False, is_error=False,
                                            patch_applied=True, is_resolved=True), RESOLVED)

    def test_unresolved_applied_but_failed_tests(self):
        self.assertEqual(classify_instance(patch_is_empty=False, is_error=False,
                                            patch_applied=True, is_resolved=False), UNRESOLVED)

    def test_apply_failure_is_not_unresolved(self):
        self.assertEqual(classify_instance(patch_is_empty=False, is_error=False,
                                            patch_applied=False, is_resolved=False), APPLY_FAILED)

    def test_empty_patch_precedence(self):
        self.assertEqual(classify_instance(patch_is_empty=True, is_error=False,
                                            patch_applied=None, is_resolved=False), EMPTY_PATCH)

    def test_error(self):
        self.assertEqual(classify_instance(patch_is_empty=False, is_error=True,
                                            patch_applied=None, is_resolved=False), ERROR)


class SummarizeTests(unittest.TestCase):
    def test_resolve_rate_excludes_apply_failures(self):
        verdicts = [
            InstanceVerdict("a", RESOLVED, resolved=True, patch_applied=True),
            InstanceVerdict("b", RESOLVED, resolved=True, patch_applied=True),
            InstanceVerdict("c", UNRESOLVED, patch_applied=True),
            InstanceVerdict("d", APPLY_FAILED, patch_applied=False),
            InstanceVerdict("e", APPLY_FAILED, patch_applied=False),
            InstanceVerdict("f", EMPTY_PATCH),
        ]
        s = summarize(verdicts)
        self.assertEqual(s.counts[APPLY_FAILED], 2)
        self.assertEqual(s.gradeable, 3)         # 2 resolved + 1 unresolved
        self.assertEqual(s.submitted, 6)
        self.assertAlmostEqual(s.resolve_rate, 2 / 3)          # apply-fails NOT in denominator
        self.assertAlmostEqual(s.resolve_rate_submitted, 2 / 6)

    def test_all_buckets_reported(self):
        s = summarize([InstanceVerdict("a", RESOLVED, resolved=True)])
        self.assertEqual(set(s.counts.keys()),
                         {RESOLVED, UNRESOLVED, APPLY_FAILED, EMPTY_PATCH, ERROR})


class ParseReportsTests(unittest.TestCase):
    def test_combines_top_and_per_instance(self):
        top = {"resolved_ids": ["a"], "error_ids": ["e"], "empty_patch_ids": ["f"]}
        per = {
            "a": {"patch_successfully_applied": True, "resolved": True},
            "c": {"patch_successfully_applied": True, "resolved": False},
            "d": {"patch_successfully_applied": False, "resolved": False},
        }
        verdicts = classify_from_reports(["a", "c", "d", "e", "f"], top, per)
        by_id = {v.instance_id: v.bucket for v in verdicts}
        self.assertEqual(by_id, {"a": RESOLVED, "c": UNRESOLVED, "d": APPLY_FAILED,
                                 "e": ERROR, "f": EMPTY_PATCH})

    def test_nested_per_instance_shape(self):
        # swebench sometimes nests the payload under the instance id
        per = {"x": {"x": {"patch_successfully_applied": False}}}
        verdicts = classify_from_reports(["x"], {}, per)
        self.assertEqual(verdicts[0].bucket, APPLY_FAILED)


if __name__ == "__main__":
    unittest.main()
