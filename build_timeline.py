#!/usr/bin/env python3
"""HIVE-340 — order a repo's issues into a chronological round timeline.

The compounding run is a repo *career*: the agent works the repo's issues in the order
they actually arose, accumulating memory as it goes, so later rounds face fresh, unseen
issues with more experience behind them. This builds the ordered instance-id list that
`run_rounds.py --timeline` consumes, and re-verifies the contamination guard for that
exact ordering.

Ordering: chronological by the issue's PR `created_at` (the SWE-bench dataset field) —
the honest "when did this issue appear" axis. Offline, fall back to the committer date of
`base_commit` (monotonic with issue era for a single repo) via git. Ties break by PR
number then instance id, so the order is fully deterministic.

Contamination guard (re-verified per sequence): the career indexes the repo code ONCE, at
the OLDEST base_commit among the timeline instances (by committer date). A SWE-bench fix +
its tests are introduced by the instance's PR strictly AFTER its base_commit, so they are
absent from that commit's history and from any ancestor of it. The guard therefore holds
iff the indexed commit is an ANCESTOR of every timeline instance's base_commit — then the
indexed tree predates every fix + test in the whole career (no leakage). If the base
commits diverge (no common-ancestor used commit), the repo can't share one index and the
builder flags it PER-INSTANCE so the career is re-scoped rather than silently leaking.

The ordering is pure and unit-tested; the HF pull and git guard are host-side (they need
`datasets` and a clone, like index_instance.py / compute_repo_versions.py).

Usage:
  python3 build_timeline.py --repo django/django --out django_timeline.json          # HF, all
  python3 build_timeline.py --repo django/django --limit 3 --out django_timeline.json # first 3
  python3 build_timeline.py --repo django/django --instances a,b,c --out t.json        # subset
  python3 build_timeline.py --repo django/django --source pilot --order committer ...  # offline
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

import compute_repo_versions as crv  # committer_ts, is_ancestor
import index_instance as idx          # DATASET, SPLIT, ensure_repo_cache, repo_slug, run

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Ordering (pure — no I/O, fully unit-testable)
# --------------------------------------------------------------------------- #

def pr_number(instance_id: str) -> int:
    """The trailing PR number in a SWE-bench instance id (…-<n>); -1 if absent. A stable,
    dataset-free tiebreak that is also monotonic with issue chronology for one repo."""
    m = re.search(r"(\d+)$", instance_id)
    return int(m.group(1)) if m else -1


def order_by_chronology(rows: list[dict]) -> list[dict]:
    """Order rows chronologically. Each row must carry a comparable ``sort_ts`` (epoch
    seconds from created_at or committer date). Deterministic: (sort_ts, pr_number,
    instance_id). Rows missing ``sort_ts`` sort last (by pr_number) so a partial dataset
    still yields a stable order rather than raising."""
    have_ts = [r for r in rows if r.get("sort_ts") is not None]
    no_ts = [r for r in rows if r.get("sort_ts") is None]
    have_ts.sort(key=lambda r: (r["sort_ts"], pr_number(r["instance_id"]), r["instance_id"]))
    no_ts.sort(key=lambda r: (pr_number(r["instance_id"]), r["instance_id"]))
    return have_ts + no_ts


def _parse_iso_epoch(value: str) -> int | None:
    """SWE-bench created_at (e.g. '2019-04-24T18:14:03Z') -> epoch seconds; None if unparseable."""
    if not value:
        return None
    try:
        return int(_dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Sources (I/O) — where the (instance_id, base_commit, created_at) rows come from
# --------------------------------------------------------------------------- #

def rows_from_hf(repo: str) -> list[dict]:
    """All of `repo`'s instances from SWE-bench Verified, with created_at + base_commit."""
    from datasets import load_dataset  # heavy; host-side only

    ds = load_dataset(idx.DATASET, split=idx.SPLIT)
    ds = ds.filter(lambda r: r["repo"] == repo)
    rows = []
    for r in ds:
        rows.append({
            "instance_id": r["instance_id"],
            "base_commit": r["base_commit"],
            "created_at": r.get("created_at"),
            "sort_ts": _parse_iso_epoch(r.get("created_at")),
        })
    return rows


def rows_from_pilot(repo: str, subset_path: str | Path = "pilot_subset.json") -> list[dict]:
    """`repo`'s instances from the committed pilot subset (no created_at — order via git)."""
    p = Path(subset_path)
    if not p.is_absolute():
        p = HERE / p
    sub = json.loads(p.read_text())
    insts = sub.get("instances", sub) if isinstance(sub, dict) else sub
    return [{"instance_id": i["instance_id"], "base_commit": i["base_commit"],
             "created_at": None, "sort_ts": None}
            for i in insts if i.get("repo") == repo]


def fill_committer_ts(repo: str, rows: list[dict], workdir: str) -> Path:
    """Populate each row's sort_ts from the git committer date of its base_commit (offline
    chronology / created_at fallback). Returns the repo cache path (reused by the guard)."""
    cache = Path(workdir) / "repos" / idx.repo_slug(repo)
    idx.ensure_repo_cache(repo, [r["base_commit"] for r in rows], cache)
    for r in rows:
        r["sort_ts"] = crv.committer_ts(cache, r["base_commit"])
    return cache


# --------------------------------------------------------------------------- #
# Guard re-verification (git ancestry) — reuses the index_instance machinery
# --------------------------------------------------------------------------- #

def verify_guard(repo: str, ordered_rows: list[dict], workdir: str,
                 cache: Path | None = None) -> dict:
    """Re-prove leakage-safety for this exact ordering. Index at the OLDEST base_commit
    (by committer date); the guard passes iff it is an ancestor of every base_commit."""
    if cache is None:
        cache = Path(workdir) / "repos" / idx.repo_slug(repo)
        idx.ensure_repo_cache(repo, [r["base_commit"] for r in ordered_rows], cache)
    commits = [r["base_commit"] for r in ordered_rows]
    oldest = sorted((crv.committer_ts(cache, c), c) for c in commits)[0][1]
    ancestors = {c: crv.is_ancestor(cache, oldest, c) for c in commits}
    ok = all(ancestors.values())
    return {
        "mode": "shared" if ok else "per-instance",
        "indexed_base_commit": oldest,
        "is_ancestor_of_all": ok,
        "verified": True,
        "non_ancestor_base_commits": [c for c, a in ancestors.items() if not a],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build(repo: str, *, source: str, order: str, instances: list[str] | None,
          limit: int | None, workdir: str, do_guard: bool) -> dict:
    rows = rows_from_hf(repo) if source == "hf" else rows_from_pilot(repo)
    if not rows:
        raise SystemExit(f"no instances for {repo} in source={source}")

    if instances:
        keep = set(instances)
        rows = [r for r in rows if r["instance_id"] in keep]
        missing = keep - {r["instance_id"] for r in rows}
        if missing:
            raise SystemExit(f"requested instances not found in {source}: {sorted(missing)}")

    cache = None
    # committer-date order (or created_at missing) needs git; also warms the guard cache.
    if order == "committer" or any(r.get("sort_ts") is None for r in rows):
        cache = fill_committer_ts(repo, rows, workdir)

    ordered = order_by_chronology(rows)
    if limit is not None:
        ordered = ordered[:limit]

    guard = verify_guard(repo, ordered, workdir, cache=cache) if do_guard else {"verified": False}

    return {
        "repo": repo,
        "dataset": idx.DATASET,
        "split": idx.SPLIT,
        "source": source,
        "ordering": "created_at" if (source == "hf" and order == "created_at") else "committer_date",
        "count": len(ordered),
        "ordered_ids": [r["instance_id"] for r in ordered],
        "instances": [{"instance_id": r["instance_id"], "base_commit": r["base_commit"],
                       "created_at": r.get("created_at")} for r in ordered],
        "guard": guard,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a repo's chronological issue timeline (HIVE-340).")
    ap.add_argument("--repo", default="django/django")
    ap.add_argument("--out", default="django_timeline.json")
    ap.add_argument("--source", choices=["hf", "pilot"], default="hf",
                    help="hf = SWE-bench Verified (authoritative created_at); pilot = committed pilot_subset.json.")
    ap.add_argument("--order", choices=["created_at", "committer"], default="created_at",
                    help="chronology axis; created_at needs hf, else falls back to git committer date.")
    ap.add_argument("--instances", default="", help="comma-separated subset (still ordered chronologically).")
    ap.add_argument("--limit", type=int, default=None, help="keep only the first N (earliest) instances.")
    ap.add_argument("--workdir", default=idx.os.environ.get("ARMB_WORKDIR", ".localdata/armb"))
    ap.add_argument("--no-verify-guard", action="store_true", help="skip the git ancestry re-check (needs a clone).")
    args = ap.parse_args()

    instances = [s for s in (i.strip() for i in args.instances.split(",")) if s] or None
    result = build(args.repo, source=args.source, order=args.order, instances=instances,
                   limit=args.limit, workdir=args.workdir, do_guard=not args.no_verify_guard)

    out = Path(args.out)
    if not out.is_absolute():
        out = HERE / out
    out.write_text(json.dumps(result, indent=2) + "\n")

    g = result["guard"]
    print(f"[timeline] {args.repo}: {result['count']} instance(s), ordering={result['ordering']}")
    print(f"[timeline] order: {result['ordered_ids']}")
    if g.get("verified"):
        if g["is_ancestor_of_all"]:
            print(f"[timeline] guard PASS — index at {g['indexed_base_commit'][:12]} "
                  f"(oldest) is an ancestor of all {result['count']} base_commit(s) [SHARED].")
        else:
            print(f"[timeline] guard FAIL — base_commits DIVERGE (PER-INSTANCE); "
                  f"non-ancestors: {[c[:12] for c in g['non_ancestor_base_commits']]}")
            print("[timeline] -> re-scope the career (this ordering cannot share one index without leakage).")
            out.write_text(json.dumps(result, indent=2) + "\n")
            return 2
    else:
        print("[timeline] guard NOT verified (--no-verify-guard).")
    print(f"[timeline] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
