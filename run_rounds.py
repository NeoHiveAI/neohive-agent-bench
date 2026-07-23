#!/usr/bin/env python3
"""HIVE-337 (rolling-rounds runner) + HIVE-338 (memoryless twin).

Orchestrates one repo *career*: take the repo's chronologically ordered instances
(HIVE-340 timeline), split them into rounds, and for each round —
  1. run the fresh, unseen slice (never seen in an earlier round),
  2. grade it on the STOCK swebench harness (HIVE-343 bucketing),
  3. the round's learnings are already written to the persistent experience pool by the
     reflect-and-store step (HIVE-335) that fired per task, and
  4. snapshot the pool (HIVE-334) so this dose can be restored / re-run,
then advance: the next round works with more accumulated memory. Treated (memory on)
vs the paired **twin** (same slices, same order, memory OFF) is the whole comparison —
treated climbs, twin flat => the rise is accumulation, not easier late issues.

The round loop takes injectable `executor` / `grader` / `snapshotter`, so the sequencing
is unit-tested and `--dry-run` exercises the whole loop with stubs (no Docker). The
default implementations shell out to `run_opencode.py`, `grade_swebench.sh` +
`grading.py`, and `hive_snapshot.py` for the real x86-host run.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import grading
import hive_snapshot as snap

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Sequencing (pure)
# --------------------------------------------------------------------------- #

def make_rounds(ordered_ids: list[str], *, n_rounds: int | None = None,
                round_size: int | None = None) -> list[list[str]]:
    """Split a chronologically ordered id list into contiguous round slices, preserving
    order (no shuffling — chronology is the point). Exactly one of `n_rounds` /
    `round_size` must be given."""
    if (n_rounds is None) == (round_size is None):
        raise ValueError("pass exactly one of n_rounds or round_size")
    ids = list(ordered_ids)
    if not ids:
        return []
    if round_size is not None:
        if round_size < 1:
            raise ValueError("round_size must be >= 1")
        return [ids[i:i + round_size] for i in range(0, len(ids), round_size)]
    if n_rounds < 1:
        raise ValueError("n_rounds must be >= 1")
    n = min(n_rounds, len(ids))
    # contiguous near-equal slices, order preserved
    base, extra = divmod(len(ids), n)
    rounds, start = [], 0
    for r in range(n):
        size = base + (1 if r < extra else 0)
        rounds.append(ids[start:start + size])
        start += size
    return rounds


# --------------------------------------------------------------------------- #
# Config + results
# --------------------------------------------------------------------------- #

@dataclass
class RoundsConfig:
    repo: str
    ordered_ids: list[str]
    mode: str                 # "treated" | "twin"
    model: str
    project: str | None       # career project id (treated and twin use DIFFERENT projects)
    hive_id: str | None = None  # experience-pool (Knowledge) hive id, for snapshotting
    data_dir: str | None = None
    n_rounds: int | None = None
    round_size: int | None = None
    output_dir: str = ""

    def rounds(self) -> list[list[str]]:
        return make_rounds(self.ordered_ids, n_rounds=self.n_rounds, round_size=self.round_size)


@dataclass
class RoundResult:
    index: int
    slice: list[str]
    grading: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)


@dataclass
class CareerResult:
    repo: str
    mode: str
    model: str
    rounds: list = field(default_factory=list)          # list[RoundResult]
    resolve_series: list = field(default_factory=list)   # per-round resolve rate (gradeable)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo, "mode": self.mode, "model": self.model,
            "resolve_series": self.resolve_series,
            "rounds": [asdict(r) for r in self.rounds],
        }


# --------------------------------------------------------------------------- #
# The round loop (injectable deps -> testable)
# --------------------------------------------------------------------------- #

def run_career(config: RoundsConfig, *, executor, grader, snapshotter) -> CareerResult:
    """Run all rounds for one career. `executor(slice, round_idx, config)` runs the
    slice; `grader(slice, round_idx, config) -> GradingSummary`; `snapshotter(round_idx,
    config) -> dict` snapshots the experience pool after the round."""
    result = CareerResult(repo=config.repo, mode=config.mode, model=config.model)
    for r, slc in enumerate(config.rounds()):
        executor(slc, r, config)                      # runs the fresh slice (writes fire per task)
        summary = grader(slc, r, config)              # stock grading + HIVE-343 buckets
        snapshot = snapshotter(r, config)             # HIVE-334 snapshot the dose after the round
        result.rounds.append(RoundResult(index=r, slice=list(slc),
                                          grading=summary.as_dict(), snapshot=snapshot))
        result.resolve_series.append(summary.resolve_rate)
    return result


# --------------------------------------------------------------------------- #
# Default (real) implementations — shell out; run on the x86 host
# --------------------------------------------------------------------------- #

def default_executor(slc, round_idx, config: RoundsConfig):
    """Run a slice through run_opencode.py in the career project. treated => arm-b
    default (compounding, no flag); twin => --twin. Same command shape for both (only
    the twin flag + project differ), so the scaffold is held constant."""
    out = Path(config.output_dir or (HERE / "results")) / f"{config.mode}-round{round_idx}"
    env = dict(os.environ, NEOHIVE_PROJECT=config.project or "")
    cmd = [str(HERE / ".venv/bin/python"), str(HERE / "run_opencode.py"),
           "--arm", "b", "--model", config.model, "--output", str(out)]
    if config.mode == "twin":
        cmd.append("--twin")   # treated relies on the compounding default
    cmd += list(slc)
    subprocess.run(cmd, check=True, env=env)
    return out / "preds.json"


def default_grader(slc, round_idx, config: RoundsConfig) -> grading.GradingSummary:
    out = Path(config.output_dir or (HERE / "results")) / f"{config.mode}-round{round_idx}"
    preds = out / "preds.json"
    run_id = f"{config.repo.replace('/', '_')}-{config.mode}-r{round_idx}"
    subprocess.run([str(HERE / "grade_swebench.sh"), str(preds), run_id, "2"], check=True, cwd=str(HERE))
    # swebench writes "<model with / -> __>.<run_id>.json" to CWD (ignores --report_dir),
    # and logs under run_evaluation/<run_id>/<model with / -> __>/ (confirmed on olympus).
    cands = sorted(HERE.glob(f"*{run_id}.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    report = cands[0] if cands else HERE / f"{config.model.replace('/', '__')}.{run_id}.json"
    logs = HERE / "logs" / "run_evaluation" / run_id / config.model.replace("/", "__")
    return grading.grade_report(report, logs_dir=logs, instance_ids=list(slc))


def default_snapshotter(round_idx, config: RoundsConfig) -> dict:
    """FS snapshot the career project's experience-pool hive after the round."""
    if not (config.data_dir and config.project and config.hive_id):
        return {"skipped": "missing data_dir/project/hive_id"}
    hdir = snap.hive_dir(config.data_dir, config.project, config.hive_id)
    dest = Path(config.output_dir or (HERE / "results")) / "snapshots" / f"{config.mode}-round{round_idx}"
    return snap.snapshot_fs(hdir, dest, live=True)


# --------------------------------------------------------------------------- #
# Dry-run stubs (offline sequencing validation)
# --------------------------------------------------------------------------- #

def _dry_run(config: RoundsConfig) -> CareerResult:
    log: list[str] = []

    def executor(slc, r, cfg):
        log.append(f"exec round{r} [{cfg.mode}] slice={slc}")

    def grader(slc, r, cfg):
        # deterministic fake: treated climbs with round, twin stays flat
        resolved = (r + 1) if cfg.mode == "treated" else 1
        verdicts = ([grading.InstanceVerdict(i, grading.RESOLVED, resolved=True) for i in slc[:resolved]]
                    + [grading.InstanceVerdict(i, grading.UNRESOLVED) for i in slc[resolved:]])
        return grading.summarize(verdicts)

    def snapshotter(r, cfg):
        log.append(f"snapshot after round{r} [{cfg.mode}]")
        return {"round": r, "dry_run": True}

    res = run_career(config, executor=executor, grader=grader, snapshotter=snapshotter)
    print("\n".join(log))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Rolling-rounds compounding runner (HIVE-337/338).")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--timeline", required=True, help="JSON file: chronologically ordered instance ids (HIVE-340).")
    ap.add_argument("--mode", choices=["treated", "twin"], required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--project", default=os.environ.get("NEOHIVE_PROJECT"))
    ap.add_argument("--hive-id", default=None, help="experience-pool (Knowledge) hive id to snapshot")
    ap.add_argument("--data-dir", default=os.environ.get("NEOHIVE_DATA_DIR"))
    ap.add_argument("--rounds", type=int, default=None)
    ap.add_argument("--round-size", type=int, default=None)
    ap.add_argument("--output", default="")
    ap.add_argument("--dry-run", action="store_true", help="exercise the loop with stubs (no Docker).")
    args = ap.parse_args()

    ordered = json.loads(Path(args.timeline).read_text())
    if isinstance(ordered, dict):
        ordered = ordered.get("ordered_ids") or ordered.get(args.repo) or []
    config = RoundsConfig(
        repo=args.repo, ordered_ids=ordered, mode=args.mode, model=args.model,
        project=args.project, hive_id=args.hive_id, data_dir=args.data_dir,
        n_rounds=args.rounds, round_size=args.round_size, output_dir=args.output,
    )
    if not config.ordered_ids:
        raise SystemExit("no instance ids in --timeline")

    if args.dry_run:
        res = _dry_run(config)
    else:
        res = run_career(config, executor=default_executor, grader=default_grader,
                         snapshotter=default_snapshotter)

    out_dir = Path(args.output) if args.output else HERE / "results" / f"career-{args.mode}-{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "career.json").write_text(json.dumps(res.as_dict(), indent=2))
    print(f"[rounds] {args.repo} [{args.mode}] resolve_series={res.resolve_series}")
    print(f"[rounds] -> {out_dir / 'career.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
