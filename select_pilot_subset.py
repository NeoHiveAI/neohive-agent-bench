#!/usr/bin/env python3
"""
HIVE-288 — Select the SWE-bench Verified pilot subset.

Deterministic, reproducible stratified sampler for the ~30-instance pilot used
to validate the NeoHive SWE-bench A/B methodology before scaling to the full
bench (HIVE-289).

Design:
  - Source of truth: the public `princeton-nlp/SWE-bench_Verified` dataset,
    pulled live from the HuggingFace datasets-server (stdlib only, no pip).
  - Stratify primarily by REPO (cap per repo) so per-instance codebase indexing
    is exercised across many distinct codebases rather than over-weighting
    django (which is 231/500 = 46% of the full set).
  - Within each repo, spread across the human-annotated `difficulty` buckets so
    the pilot is not all-trivial.
  - Fixed SEED -> the same 30 instances every run. Re-run to regenerate
    pilot_subset.json verbatim.

Usage:
    python3 select_pilot_subset.py            # prints the subset, writes pilot_subset.json
"""
import json
import urllib.request
import random
import collections

DATASET = "princeton-nlp%2FSWE-bench_Verified"
BASE = ("https://datasets-server.huggingface.co/rows?dataset=" + DATASET +
        "&config=default&split=test")
SEED = 42
TARGET = 30
PER_REPO_CAP = 4   # ensure breadth: no single repo dominates the pilot
PER_REPO_MIN = 1
DIFF_ORDER = ["<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours", "unknown"]


def fetch_all():
    rows = []
    for off in range(0, 500, 100):
        with urllib.request.urlopen(f"{BASE}&offset={off}&length=100", timeout=60) as r:
            data = json.load(r)
        for item in data["rows"]:
            row = item["row"]
            rows.append({
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "difficulty": row.get("difficulty", "unknown"),
                "base_commit": row["base_commit"],
            })
    return rows


def diff_key(d):
    return DIFF_ORDER.index(d) if d in DIFF_ORDER else len(DIFF_ORDER)


def main():
    rows = fetch_all()
    assert len(rows) == 500, f"expected 500 instances, got {len(rows)}"
    by_repo = collections.Counter(r["repo"] for r in rows)
    rng = random.Random(SEED)
    repos_sorted = [r for r, _ in by_repo.most_common()]  # largest first, stable

    # proportional allocation, clamped to [MIN, CAP], then adjusted to exactly TARGET
    alloc = {repo: max(PER_REPO_MIN, min(PER_REPO_CAP, round(TARGET * by_repo[repo] / len(rows))))
             for repo in repos_sorted}
    total = lambda: sum(alloc.values())
    while total() > TARGET:
        for repo in repos_sorted:
            if total() == TARGET:
                break
            if alloc[repo] > PER_REPO_MIN:
                alloc[repo] -= 1
    while total() < TARGET:
        for repo in repos_sorted:
            if total() == TARGET:
                break
            if alloc[repo] < min(PER_REPO_CAP, by_repo[repo]):
                alloc[repo] += 1

    grouped = collections.defaultdict(list)
    for r in rows:
        grouped[r["repo"]].append(r)

    selected = []
    for repo in repos_sorted:
        want = alloc[repo]
        buckets = collections.defaultdict(list)
        for r in grouped[repo]:
            buckets[r["difficulty"]].append(r)
        for b in buckets.values():
            b.sort(key=lambda r: r["instance_id"])
            rng.shuffle(b)  # seeded
        order = sorted(buckets.keys(), key=diff_key)
        i = 0
        picked = []
        while len(picked) < want and any(buckets[o] for o in order):
            o = order[i % len(order)]
            if buckets[o]:
                picked.append(buckets[o].pop(0))
            i += 1
        selected.extend(picked)

    selected.sort(key=lambda r: (r["repo"], r["instance_id"]))
    out = {"seed": SEED, "target": TARGET, "count": len(selected),
           "repos": len({s["repo"] for s in selected}), "instances": selected}
    with open("pilot_subset.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"PILOT SUBSET: {len(selected)} instances across {out['repos']} repos (seed={SEED})")
    for s in selected:
        print(f"  {s['instance_id']:35s} {s['difficulty']:18s} {s['repo']}")
    print("wrote pilot_subset.json")


if __name__ == "__main__":
    main()
