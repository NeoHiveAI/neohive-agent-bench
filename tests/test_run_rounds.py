"""HIVE-337/338 — tests for the rolling-rounds runner + twin sequencing.

Proves: chronological slicing (order preserved, no shuffle), the round loop grades then
snapshots each round, and treated + twin see IDENTICAL slices in identical order.
Stdlib only.
"""
from __future__ import annotations

import unittest

import grading
from run_rounds import RoundsConfig, make_rounds, run_career


class MakeRoundsTests(unittest.TestCase):
    def test_round_size(self):
        self.assertEqual(make_rounds(list("abcde"), round_size=2),
                         [["a", "b"], ["c", "d"], ["e"]])

    def test_n_rounds_contiguous_near_equal(self):
        self.assertEqual(make_rounds(list("abcde"), n_rounds=3),
                         [["a", "b"], ["c", "d"], ["e"]])

    def test_order_preserved(self):
        ids = [f"i{n}" for n in range(6)]
        flat = [x for rnd in make_rounds(ids, n_rounds=3) for x in rnd]
        self.assertEqual(flat, ids)  # chronology intact

    def test_n_rounds_exceeds_len(self):
        self.assertEqual(make_rounds(["a", "b"], n_rounds=5), [["a"], ["b"]])

    def test_empty(self):
        self.assertEqual(make_rounds([], n_rounds=3), [])

    def test_requires_exactly_one_knob(self):
        with self.assertRaises(ValueError):
            make_rounds(list("abc"))
        with self.assertRaises(ValueError):
            make_rounds(list("abc"), n_rounds=2, round_size=2)


class RunCareerTests(unittest.TestCase):
    def _config(self, mode):
        return RoundsConfig(repo="django/django", ordered_ids=[f"i{n}" for n in range(6)],
                            mode=mode, model="z-ai/glm-5.2", project=f"proj-{mode}", round_size=2)

    def test_loop_grades_and_snapshots_each_round(self):
        cfg = self._config("treated")
        calls = {"exec": [], "snap": []}

        def executor(slc, r, c):
            calls["exec"].append((r, list(slc)))

        def grader(slc, r, c):
            resolved = r + 1  # treated climbs
            v = ([grading.InstanceVerdict(i, grading.RESOLVED, resolved=True) for i in slc[:resolved]]
                 + [grading.InstanceVerdict(i, grading.UNRESOLVED) for i in slc[resolved:]])
            return grading.summarize(v)

        def snapshotter(r, c):
            calls["snap"].append(r)
            return {"round": r}

        res = run_career(cfg, executor=executor, grader=grader, snapshotter=snapshotter)
        self.assertEqual(len(res.rounds), 3)
        self.assertEqual(calls["snap"], [0, 1, 2])                 # snapshot after every round
        self.assertEqual([e[0] for e in calls["exec"]], [0, 1, 2])
        self.assertEqual(len(res.resolve_series), 3)
        # slices are the contiguous chronological rounds
        self.assertEqual(res.rounds[0].slice, ["i0", "i1"])
        self.assertEqual(res.rounds[2].slice, ["i4", "i5"])

    def test_twin_sees_identical_slices(self):
        treated = self._config("treated")
        twin = self._config("twin")
        self.assertEqual(treated.rounds(), twin.rounds())  # same slices, same order


if __name__ == "__main__":
    unittest.main()
