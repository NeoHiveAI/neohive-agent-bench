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

import json
from dataclasses import dataclass
from pathlib import Path


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


# --------------------------------------------------------------------------- #
# Report CLI — treated-vs-twin improvement curve from two run_rounds careers
# --------------------------------------------------------------------------- #

def series_from_career(career: dict) -> list[float]:
    """Per-round resolve-rate series from a run_rounds `career.json`. Prefers the stored
    `resolve_series`; else recomputes from each round's HIVE-343 `resolve_rate` (resolved
    over gradeable, apply-failures excluded), so the report is robust to either shape."""
    if career.get("resolve_series"):
        return list(career["resolve_series"])
    return [r.get("grading", {}).get("resolve_rate", 0.0) for r in career.get("rounds", [])]


def build_report(treated: dict, twin: dict, *, R: list[list[float]] | None = None,
                 baseline: list[float] | None = None) -> dict:
    """Assemble the full report payload from a treated + a twin career. The primary signal
    is the treated-vs-twin improvement curve; GEM metrics are included only when a full
    task×task matrix R (+ baseline) is supplied (dose restore + replay, not the online run)."""
    t_series, w_series = series_from_career(treated), series_from_career(twin)
    curve = improvement_curve(t_series, w_series)
    payload = {
        "repo": treated.get("repo") or twin.get("repo"),
        "model": treated.get("model"),
        "treated": {"mode": treated.get("mode", "treated"), "resolve_series": curve.treated,
                    "slope": curve.treated_slope},
        "twin": {"mode": twin.get("mode", "twin"), "resolve_series": curve.twin,
                 "slope": curve.twin_slope},
        "per_round_gap": curve.per_round_gap,
        "mean_gap": curve.mean_gap,
        # The headline read: treated slope > 0 AND above the (flat) twin => rise is
        # accumulation, not later rounds drawing easier issues.
        "compounding_signal": curve.treated_slope > 0 and curve.mean_gap > 0,
    }
    if R is not None and baseline is not None:
        payload["gem"] = gem_metrics(R, baseline)
    return payload


def render_report(payload: dict) -> str:
    """Human-readable rendering of build_report()'s payload."""
    def fmt(series):
        return "[" + ", ".join(f"{x:.3f}" for x in series) + "]"

    lines = [
        "treated-vs-twin improvement curve (HIVE-342)",
        f"  repo:  {payload.get('repo')}    model: {payload.get('model')}",
        f"  treated ({payload['treated']['mode']}): {fmt(payload['treated']['resolve_series'])}  slope={payload['treated']['slope']:+.4f}",
        f"  twin    ({payload['twin']['mode']}): {fmt(payload['twin']['resolve_series'])}  slope={payload['twin']['slope']:+.4f}",
        f"  per-round gap: {fmt(payload['per_round_gap'])}",
        f"  mean gap (treated - twin): {payload['mean_gap']:+.4f}",
        f"  compounding signal (treated rising AND above twin): {'YES' if payload['compounding_signal'] else 'no'}",
    ]
    if "gem" in payload:
        g = payload["gem"]
        lines += [
            "  GEM metrics (Lopez-Paz & Ranzato 2017):",
            f"    ACC (avg accuracy, final round):     {g['average_accuracy']:+.4f}",
            f"    BWT (backward transfer):             {g['backward_transfer']:+.4f}",
            f"    FWT (forward transfer vs baseline):  {g['forward_transfer']:+.4f}",
            f"    forgetting (max(0,-BWT)):            {g['forgetting']:.4f}",
        ]
    return "\n".join(lines)


def _load_json(path: str | Path):
    return json.loads(Path(path).read_text())


def main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Treated-vs-twin improvement-curve report (HIVE-342).")
    ap.add_argument("--treated", required=True, help="treated career.json (run_rounds --mode treated).")
    ap.add_argument("--twin", required=True, help="twin career.json (run_rounds --mode twin).")
    ap.add_argument("--matrix", default=None, help="optional task×task matrix R JSON (enables GEM).")
    ap.add_argument("--baseline", default=None, help="optional no-memory baseline series JSON (with --matrix).")
    ap.add_argument("--json", action="store_true", help="emit the report payload as JSON.")
    ap.add_argument("--out", default="", help="also write the JSON payload to this path.")
    args = ap.parse_args()

    R = _load_json(args.matrix) if args.matrix else None
    baseline = _load_json(args.baseline) if args.baseline else None
    if (R is None) != (baseline is None):
        raise SystemExit("--matrix and --baseline must be given together (GEM needs both).")

    payload = build_report(_load_json(args.treated), _load_json(args.twin), R=R, baseline=baseline)
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2) if args.json else render_report(payload))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
