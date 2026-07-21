#!/usr/bin/env python3
"""
Compute, per repo, how Arm B indexes it — leakage-safe — and emit a routing artifact.

Rationale (leakage): the pilot uses several instances per repo, each pinned at a
different base_commit. If we indexed an instance's own base_commit and let an OLDER
instance of the same repo retrieve from that (newer) index, future code could leak an
earlier instance's fix. A SWE-bench fix + its tests are introduced by the instance's PR
strictly AFTER its base_commit, so they are absent from base_commit's history and from
any ANCESTOR of it. So a hive is safe for an instance iff the indexed commit is an
ancestor of that instance's base_commit.

Two modes per repo:
  - SHARED: if the oldest base_commit (by committer date) is an ANCESTOR of every one of
    the repo's instances, index that single oldest-used commit once and route all the
    repo's instances to it. One hive, reusable, at a real used version. (11/12 repos.)
  - PER-INSTANCE: if the repo's base_commits DIVERGE (each on a different release branch,
    so no single used commit is a common ancestor), index EACH instance at its OWN
    base_commit in its own hive and route each instance only to itself. A commit is an
    ancestor of itself, so this is trivially leakage-safe and gives each instance a
    representative index. (matplotlib: its 3 instances are each on a different release
    line.) This is the per-version-per-run idea, applied only where sharing is impossible.

Output: pilot_repo_versions.json — the committed transparency artifact. Per repo it
records the mode and, per instance, which commit is indexed and which hive it routes to.
Branch on each NeoHiveAI/swebench-mirror-<repo>: `swebench-base` (shared) or
`swebench/<instance_id>` (per-instance), pinned at the indexed commit.
Run on a host with git + network (the Mac); commit the result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import index_instance as idx  # ensure_repo_cache, run, repo_slug

HERE = Path(__file__).resolve().parent


def committer_ts(cache: Path, sha: str) -> int:
    return int(idx.run(["git", "-C", str(cache), "show", "-s", "--format=%ct", sha]).stdout.strip())


def is_ancestor(cache: Path, ancestor: str, descendant: str) -> bool:
    return idx.run(["git", "-C", str(cache), "merge-base", "--is-ancestor", ancestor, descendant],
                   check=False).returncode == 0


def main() -> int:
    subset = json.loads((HERE / "pilot_subset.json").read_text())
    by_repo: dict[str, list[dict]] = {}
    for inst in subset["instances"]:
        by_repo.setdefault(inst["repo"], []).append(inst)

    workdir = Path(idx.os.environ.get("ARMB_WORKDIR", ".localdata/armb"))
    out_repos = []
    for repo in sorted(by_repo):
        insts = by_repo[repo]
        commits = [i["base_commit"] for i in insts]
        cache = workdir / "repos" / idx.repo_slug(repo)
        print(f"[versions] {repo}: {len(insts)} instance(s); ensuring commits present ...")
        idx.ensure_repo_cache(repo, commits, cache)

        oldest = sorted((committer_ts(cache, c), c) for c in commits)[0][1]
        shared = all(is_ancestor(cache, oldest, c) for c in commits)

        if shared:
            rows = [{
                "instance_id": i["instance_id"],
                "base_commit": i["base_commit"],
                "indexed_base_commit": oldest,
                "hive_name": idx.repo_slug(repo),
                "branch": idx.BASE_BRANCH,
                "is_ancestor_of_base": True,  # verified in the `shared` all(...) above
            } for i in insts]
            record = {"repo": repo, "mode": "shared", "indexed_base_commit": oldest,
                      "hive_name": idx.repo_slug(repo), "instances": rows}
            print(f"[versions] {repo}: SHARED at {oldest[:12]} (oldest-used) covers {len(rows)} instance(s)")
        else:
            rows = [{
                "instance_id": i["instance_id"],
                "base_commit": i["base_commit"],
                "indexed_base_commit": i["base_commit"],  # index each at its own base
                "hive_name": i["instance_id"],
                "branch": f"{idx.SYNTH_BRANCH_PREFIX}/{i['instance_id']}",
                "is_ancestor_of_base": True,  # a commit is an ancestor of itself
            } for i in insts]
            record = {"repo": repo, "mode": "per-instance", "instances": rows}
            print(f"[versions] {repo}: PER-INSTANCE (base_commits diverge) — {len(rows)} own hives")

        out_repos.append(record)

    out = {
        "generated_from": "pilot_subset.json",
        "dataset": idx.DATASET,
        "split": idx.SPLIT,
        "note": "Arm B indexing/routing per repo. Every instance routes to a hive whose "
                "indexed commit is an ANCESTOR of that instance's base_commit, so the indexed "
                "tree predates the instance's fix and tests (no leakage). SHARED repos use one "
                "hive at the oldest-used base_commit; PER-INSTANCE repos (divergent release "
                "branches) index each instance at its own base in its own hive.",
        "count": len(out_repos),
        "repos": out_repos,
    }
    (HERE / "pilot_repo_versions.json").write_text(json.dumps(out, indent=2) + "\n")
    n_shared = sum(1 for r in out_repos if r["mode"] == "shared")
    n_hives = sum(1 if r["mode"] == "shared" else len(r["instances"]) for r in out_repos)
    print(f"[versions] wrote pilot_repo_versions.json — {len(out_repos)} repos "
          f"({n_shared} shared, {len(out_repos) - n_shared} per-instance), {n_hives} hives total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
