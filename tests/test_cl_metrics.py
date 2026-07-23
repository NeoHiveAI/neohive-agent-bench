"""HIVE-342 — tests for continual-learning metrics (GEM defs + online trend).

Values are hand-computed against Lopez-Paz & Ranzato (2017) definitions. Stdlib only.
"""
from __future__ import annotations

import math
import unittest

from cl_metrics import (
    average_accuracy,
    backward_transfer,
    build_report,
    effort_per_solve,
    forgetting,
    forward_transfer,
    gem_metrics,
    improvement_curve,
    ols_slope,
    render_report,
    resolve_rate,
    series_from_career,
)

# R[i][j] = resolve rate on slice j after accumulating through round i
R = [
    [0.50, 0.10, 0.00],
    [0.60, 0.70, 0.20],
    [0.55, 0.65, 0.80],
]
BASELINE = [0.10, 0.10, 0.10]  # twin (no-memory) resolve rate per slice


class OnlineTrendTests(unittest.TestCase):
    def test_resolve_rate_excludes_apply_failures(self):
        # 3 resolved out of 8 gradeable (apply-fails already excluded from `graded`)
        self.assertAlmostEqual(resolve_rate(3, 8), 0.375)
        self.assertEqual(resolve_rate(0, 0), 0.0)

    def test_slope_rising_and_flat(self):
        self.assertAlmostEqual(ols_slope([0.2, 0.35, 0.5]), 0.15)
        self.assertAlmostEqual(ols_slope([0.4, 0.4, 0.4]), 0.0)
        self.assertEqual(ols_slope([0.4]), 0.0)

    def test_effort_per_solve(self):
        self.assertAlmostEqual(effort_per_solve(3000.0, 6), 500.0)
        self.assertTrue(math.isinf(effort_per_solve(3000.0, 0)))

    def test_improvement_curve_gap_and_slopes(self):
        treated = [0.2, 0.4, 0.6]
        twin = [0.2, 0.25, 0.2]
        rep = improvement_curve(treated, twin)
        self.assertAlmostEqual(rep.treated_slope, 0.2)
        self.assertAlmostEqual(rep.twin_slope, 0.0)
        for got, want in zip(rep.per_round_gap, [0.0, 0.15, 0.4]):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(rep.mean_gap, 0.55 / 3)

    def test_improvement_curve_requires_pairing(self):
        with self.assertRaises(ValueError):
            improvement_curve([0.1, 0.2], [0.1])


class GemMetricTests(unittest.TestCase):
    def test_average_accuracy(self):
        self.assertAlmostEqual(average_accuracy(R), (0.55 + 0.65 + 0.80) / 3)

    def test_backward_transfer(self):
        # ((0.55-0.50) + (0.65-0.70)) / 2 = 0.0
        self.assertAlmostEqual(backward_transfer(R), 0.0)

    def test_forward_transfer(self):
        # ((R[0][1]-b1) + (R[1][2]-b2)) / 2 = ((0.10-0.10)+(0.20-0.10))/2 = 0.05
        self.assertAlmostEqual(forward_transfer(R, BASELINE), 0.05)

    def test_forgetting_nonnegative(self):
        self.assertAlmostEqual(forgetting(R), 0.0)
        # a matrix with real forgetting: later round drops slice 0
        Rf = [[0.8, 0.0], [0.3, 0.7]]
        self.assertAlmostEqual(backward_transfer(Rf), 0.3 - 0.8)  # -0.5
        self.assertAlmostEqual(forgetting(Rf), 0.5)

    def test_gem_metrics_bundle(self):
        m = gem_metrics(R, BASELINE)
        self.assertAlmostEqual(m["average_accuracy"], (0.55 + 0.65 + 0.80) / 3)
        self.assertAlmostEqual(m["forward_transfer"], 0.05)
        self.assertAlmostEqual(m["backward_transfer"], 0.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            average_accuracy([[0.1, 0.2]])  # non-square
        with self.assertRaises(ValueError):
            forward_transfer(R, [0.1, 0.1])  # baseline wrong length


class ReportTests(unittest.TestCase):
    """HIVE-342 report CLI: treated + twin careers -> improvement-curve payload."""

    TREATED = {"repo": "django/django", "mode": "treated", "model": "z-ai/glm-5.2",
               "resolve_series": [0.2, 0.4, 0.6]}
    TWIN = {"repo": "django/django", "mode": "twin", "model": "z-ai/glm-5.2",
            "resolve_series": [0.2, 0.25, 0.2]}

    def test_series_prefers_resolve_series(self):
        self.assertEqual(series_from_career(self.TREATED), [0.2, 0.4, 0.6])

    def test_series_recomputes_from_rounds_when_no_series(self):
        career = {"rounds": [{"grading": {"resolve_rate": 0.3}},
                             {"grading": {"resolve_rate": 0.5}}]}
        self.assertEqual(series_from_career(career), [0.3, 0.5])

    def test_report_slopes_gap_and_signal(self):
        payload = build_report(self.TREATED, self.TWIN)
        self.assertAlmostEqual(payload["treated"]["slope"], 0.2)
        self.assertAlmostEqual(payload["twin"]["slope"], 0.0)
        self.assertAlmostEqual(payload["mean_gap"], 0.55 / 3)
        # treated rising AND above the flat twin => the compounding read fires
        self.assertTrue(payload["compounding_signal"])

    def test_flat_treated_gives_no_signal(self):
        flat = {"repo": "django/django", "mode": "treated", "resolve_series": [0.3, 0.3, 0.3]}
        payload = build_report(flat, self.TWIN)
        self.assertFalse(payload["compounding_signal"])

    def test_report_includes_gem_when_matrix_given(self):
        payload = build_report(self.TREATED, self.TWIN, R=R, baseline=BASELINE)
        self.assertIn("gem", payload)
        self.assertAlmostEqual(payload["gem"]["forward_transfer"], 0.05)

    def test_render_is_stringy_and_mentions_both_arms(self):
        text = render_report(build_report(self.TREATED, self.TWIN))
        self.assertIn("treated", text)
        self.assertIn("twin", text)
        self.assertIn("compounding signal", text)


if __name__ == "__main__":
    unittest.main()
