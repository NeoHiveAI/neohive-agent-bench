#!/usr/bin/env python3
"""
HIVE-268 — Index one SWE-bench instance's repo @ base_commit into NeoHive (Arm B).

The contamination guard is *structural*, not a filter: both the gold `patch`
(the fix) and the `test_patch` (the tests that check the fix) are diffs applied
ON TOP of `base_commit` — the fix at agent time, the tests at grade time. So the
pristine `base_commit` tree contains neither. We therefore index the repo exactly
at `base_commit` and assert that neither patch is already present.

NeoHive's git sync indexes a *branch tip*, not an arbitrary commit
(server `services/sync/index.ts` -> `cloneRepo(repoUrl, branch)`). To pin
`base_commit` we make a local clone, create a synthetic branch `swebench-base`
pointing at `base_commit`, and point a NeoHive sync-config at that local repo
(`file://...`, branch `swebench-base`). This reuses NeoHive's real code-embedding
path (the whole point of Arm B) without any GitHub remote or credentials.

Pipeline:
  1. Load the instance row (repo, base_commit, patch, test_patch) from the dataset.
  2. Clone repo, checkout base_commit, create branch `swebench-base`.
  3. CONTAMINATION GUARD: assert the gold patch + test_patch APPLY forward
     (we are pre-fix) and do NOT apply in reverse (the answer is absent). Abort otherwise.
  4. NeoHive: create a per-instance `repo` hive, create+trigger a sync-config
     pointed at the local mirror, poll until indexed. Print the hive id.

Deterministic steps 1-3 need no NeoHive and are validated by `--guard-only`.
Step 4 needs a reachable NeoHive (NEOHIVE_BASE + NEOHIVE_PROJECT + NEOHIVE_PAT).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATASET = "princeton-nlp/SWE-Bench_Verified"
SPLIT = "test"
SYNTH_BRANCH = "swebench-base"

# Defense-in-depth: even though base_commit structurally excludes the answer,
# blocklist test dirs so retrieval can't surface pre-existing tests that hint at
# expected behaviour. The structural guard is the real guarantee; this is belt.
DEFAULT_TEST_BLOCKLIST = ["**/test/**", "**/tests/**", "**/testing/**", "**/*_test.py", "**/test_*.py"]


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def load_instance(instance_id: str) -> dict:
    """Load one instance's full row (incl. patch + test_patch) from the dataset."""
    from datasets import load_dataset  # heavy; host-side only, never in-container

    ds = load_dataset(DATASET, split=SPLIT)
    rows = ds.filter(lambda r: r["instance_id"] == instance_id)
    if len(rows) == 0:
        raise SystemExit(f"instance not found in {DATASET}:{SPLIT}: {instance_id}")
    return rows[0]


def clone_at_base(repo: str, base_commit: str, dest: Path, blobless: bool) -> None:
    url = f"https://github.com/{repo}.git"
    if dest.exists():
        run(["rm", "-rf", str(dest)])
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--no-single-branch"]
    if blobless:
        args += ["--filter=blob:none"]
    args += [url, str(dest)]
    print(f"[index] cloning {url} ({'blobless' if blobless else 'full'}) ...")
    run(args)
    run(["git", "-C", str(dest), "checkout", "--quiet", base_commit])
    run(["git", "-C", str(dest), "checkout", "--quiet", "-B", SYNTH_BRANCH])
    head = run(["git", "-C", str(dest), "rev-parse", "HEAD"]).stdout.strip()
    assert head == base_commit, f"HEAD {head} != base_commit {base_commit}"
    print(f"[index] {repo} @ {base_commit[:12]} on branch {SYNTH_BRANCH}")


def _applies(repo_dir: Path, patch_text: str, reverse: bool) -> bool:
    """True if `git apply --check [--reverse]` succeeds for patch_text."""
    if not patch_text.strip():
        return False
    args = ["git", "-C", str(repo_dir), "apply", "--check"]
    if reverse:
        args.append("--reverse")
    args.append("-")
    p = subprocess.run(args, input=patch_text, text=True, capture_output=True)
    return p.returncode == 0


def contamination_guard(repo_dir: Path, gold_patch: str, test_patch: str) -> None:
    """Assert base_commit is pre-fix: each patch applies forward, not in reverse."""
    failures = []
    for label, patch in [("gold patch", gold_patch), ("test_patch", test_patch)]:
        fwd = _applies(repo_dir, patch, reverse=False)
        rev = _applies(repo_dir, patch, reverse=True)
        # Pre-fix state: the diff can be applied (fwd) and is not already present (not rev).
        ok = fwd and not rev
        status = "OK" if ok else "CONTAMINATED"
        print(f"[guard] {label:11} applies_forward={fwd} already_present={rev} -> {status}")
        if not ok:
            failures.append(f"{label} (forward={fwd}, reverse={rev})")
    if failures:
        raise SystemExit(
            "[guard] CONTAMINATION GUARD FAILED — the indexed tree is not the clean "
            f"pre-fix base_commit state: {', '.join(failures)}"
        )
    print("[guard] PASS — base_commit tree contains neither the fix nor its tests.")


# ---- NeoHive ingestion (needs a reachable NeoHive). Verify headers/file:// on first live run. ----

def _neohive_req(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ["NEOHIVE_BASE"].rstrip("/")
    project = os.environ["NEOHIVE_PROJECT"]
    pat = os.environ.get("NEOHIVE_PAT", "")
    headers = {"Content-Type": "application/json", "X-HiveMind-Id": project}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def index_into_neohive(instance_id: str, repo: str, mirror: Path, poll_timeout: int) -> str:
    embedding_model = os.environ.get("NEOHIVE_EMBEDDING_MODEL")  # None -> server's repo default
    hive = _neohive_req("POST", "/api/hives", {
        "name": f"swebench-{instance_id}",
        "type": "repo",
        **({"embedding_model": embedding_model} if embedding_model else {}),
        "description": f"SWE-bench Arm B: {repo} @ base_commit (instance {instance_id})",
    })
    hive_id = hive["id"]
    print(f"[index] created hive {hive_id}")

    cfg = _neohive_req("POST", f"/api/hives/{hive_id}/sync-configs", {
        "repo_url": f"file://{mirror.resolve()}",
        "branch": SYNTH_BRANCH,
        "file_blocklist": DEFAULT_TEST_BLOCKLIST,
        "sync_interval_minutes": 0,  # one-shot; we trigger the initial sync below
    })
    cfg_id = cfg["id"]
    print(f"[index] created sync-config {cfg_id} (repo_url=file://{mirror}, branch={SYNTH_BRANCH})")

    # POST /sync-configs auto-dispatches an initial sync; poll until indexed.
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        cur = _neohive_req("GET", f"/api/hives/{hive_id}/sync-configs/{cfg_id}")
        sha = cur.get("last_indexed_sha")
        if sha:
            print(f"[index] READY — hive {hive_id} indexed at {sha[:12]}")
            return hive_id
        time.sleep(3)
    raise SystemExit(f"[index] sync did not reach readiness within {poll_timeout}s (hive {hive_id})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Index one SWE-bench instance @ base_commit into NeoHive (Arm B).")
    ap.add_argument("instance_id")
    ap.add_argument("--workdir", default=os.environ.get("ARMB_WORKDIR", ".localdata/armb"))
    ap.add_argument("--guard-only", action="store_true",
                    help="Run clone + contamination guard only (no NeoHive needed).")
    ap.add_argument("--blobless", action="store_true",
                    help="Faster blobless clone; OK for --guard-only, NOT for real indexing.")
    ap.add_argument("--poll-timeout", type=int, default=900)
    args = ap.parse_args()

    inst = load_instance(args.instance_id)
    repo, base_commit = inst["repo"], inst["base_commit"]
    mirror = Path(args.workdir) / args.instance_id / "repo"

    clone_at_base(repo, base_commit, mirror, blobless=args.blobless or args.guard_only)
    contamination_guard(mirror, inst.get("patch", ""), inst.get("test_patch", ""))

    if args.guard_only:
        print(f"[index] guard-only OK for {args.instance_id} (no NeoHive ingestion).")
        return 0

    hive_id = index_into_neohive(args.instance_id, repo, mirror, args.poll_timeout)
    print(f"NEOHIVE_HIVE={hive_id}")  # consumed by run_arm_b.sh
    return 0


if __name__ == "__main__":
    sys.exit(main())
