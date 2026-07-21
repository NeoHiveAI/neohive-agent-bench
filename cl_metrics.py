#!/usr/bin/env python3
"""HIVE-342 — continual-learning metrics for the compounding run.

Turns the rounds' raw results into a defensible trend. Two layers:

1. **The headline "gets better with use" signal (primary, from the online run):**
   per-round resolve rate on each fresh unseen slice, its slope, and the gap to the
   paired memory-off twin. Treated climbs + twin flat => the rise is accumulation, not
   later rounds drawing easier issues.

2. **The GEM continual-learning metrics (Lopez-Paz & Ranzato, NeurIPS 2017):** average
   accuracy (ACC), backward transfer (BWT; forgetting is negative BWT — the
   "no-maintenance" half), and forward transfer (FWT; earlier experience lifting later
   unseen tasks — the "gets better with use" half). These need the full task×task
   matrix R (R[i][j] = resolve rate on slice j after accumulating through round i),
   whose off-diagonal entries come from restoring a dose (HIVE-334) and re-running an
   earlier slice; b[j] is the baseline resolve rate on slice j with no memory (the twin).

All pure functions, stdlib only — fully unit-testable without a live run.
"""
from __future__ import annotations

from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Primary online trend
# --------------------------------------------------------------------------- #

def resolve_rate(resolved: int, graded: int) -> float:
    """Resolved / gradeable. `graded` should EXCLUDE the patch-apply-failure bucket
    (HIVE-343): an apply failure is a harness artefact, not an unresolved solve."""
    return (resolved / graded) if graded else 0.0


def ols_slope(series: list[float]) -> float:
    """Least-squares slope of `series` vs its index (the improvement-curve slope).
    0 for a flat series; >0 means rising resolve rate across rounds."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(series) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, series))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def effort_per_solve(effort_total: float, resolved: int) -> float:
    """Mean effort (tokens, tool-calls, or wall-seconds) per resolved instance in a
    round. Falling effort-per-solve across rounds is a secondary efficiency signal."""
    return (effort_total / resolved) if resolved else float("inf")


@dataclass
class TrendReport:
    treated: list[float]
    twin: list[float]
    treated_slope: float
    twin_slope: float
    per_round_gap: list[float]
    mean_gap: float


def improvement_curve(treated: list[float], twin: list[float]) -> TrendReport:
    """The treated-vs-twin resolve-rate curves + slopes + gap. `treated` and `twin`
    are per-round resolve rates over the SAME slices in the SAME order."""
    if len(treated) != len(twin):
        raise ValueError(f"treated ({len(treated)}) and twin ({len(twin)}) must be paired per round")
    gap = [t - w for t, w in zip(treated, twin)]
    return TrendReport(
        treated=treated, twin=twin,
        treated_slope=ols_slope(treated), twin_slope=ols_slope(twin),
        per_round_gap=gap, mean_gap=(sum(gap) / len(gap) if gap else 0.0),
    )


# --------------------------------------------------------------------------- #
# GEM metrics (Lopez-Paz & Ranzato 2017) on the task×task matrix
# --------------------------------------------------------------------------- #

def _validate_matrix(R: list[list[float]]) -> int:
    t = len(R)
    if t == 0 or any(len(row) != t for row in R):
        raise ValueError("R must be a non-empty square TxT matrix")
    return t


def average_accuracy(R: list[list[float]]) -> float:
    """ACC = mean over tasks of R[T-1][j] — resolve rate on every slice after the final
    round (how good the fully-accumulated agent is across all slices)."""
    t = _validate_matrix(R)
    return sum(R[t - 1]) / t


def backward_transfer(R: list[list[float]]) -> float:
    """BWT = mean_{j<T-1} (R[T-1][j] - R[j][j]). Positive = later learning helped earlier
    slices; negative = forgetting. The "no-maintenance" half of the story."""
    t = _validate_matrix(R)
    if t < 2:
        return 0.0
    return sum(R[t - 1][j] - R[j][j] for j in range(t - 1)) / (t - 1)


def forward_transfer(R: list[list[float]], baseline: list[float]) -> float:
    """FWT = mean_{j>=1} (R[j-1][j] - baseline[j]). How much accumulated memory lifts a
    later, still-unseen slice above the no-memory baseline (the twin). The "gets better
    with use" half."""
    t = _validate_matrix(R)
    if len(baseline) != t:
        raise ValueError("baseline length must equal T")
    if t < 2:
        return 0.0
    return sum(R[j - 1][j] - baseline[j] for j in range(1, t)) / (t - 1)


def forgetting(R: list[list[float]]) -> float:
    """Positive scalar amount forgotten = max(0, -BWT). 0 when nothing was forgotten."""
    return max(0.0, -backward_transfer(R))


def gem_metrics(R: list[list[float]], baseline: list[float]) -> dict:
    """All GEM metrics at once."""
    return {
        "average_accuracy": average_accuracy(R),
        "backward_transfer": backward_transfer(R),
        "forward_transfer": forward_transfer(R, baseline),
        "forgetting": forgetting(R),
    }
