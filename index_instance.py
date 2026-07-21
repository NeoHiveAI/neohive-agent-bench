#!/usr/bin/env python3
"""
HIVE-268 — Index SWE-bench repos into NeoHive (Arm B), leakage-safe, with reuse.

The pilot uses several instances per repo, each pinned at a different base_commit.
Indexing an instance's own base_commit and letting an OLDER instance of the same repo
retrieve from that (newer) index would leak future code — a later commit can already
contain an earlier instance's fix. A hive is safe for an instance iff the indexed commit
is an ANCESTOR of that instance's base_commit (a SWE-bench fix + its tests are introduced
by the instance's PR strictly AFTER base_commit, so they are absent from base_commit's
history and from any ancestor of it).

compute_repo_versions.py precomputes, per repo, one of two modes into the committed
artifact pilot_repo_versions.json:
  - SHARED: one hive at the repo's OLDEST used base_commit (an ancestor of all the repo's
    instances); every instance of that repo routes to it. Hive name = repo slug.
  - PER-INSTANCE: for repos whose base_commits diverge (different release branches), each
    instance is indexed at its OWN base_commit in its own hive, routed only to itself.
    Hive name = instance_id. Trivially leakage-safe (a commit is an ancestor of itself).

This script consumes that artifact: `index_instance.py <instance_id>` looks up the
instance's plan, indexes/reuses the target hive, re-asserts ancestry on the indexing host
as defense-in-depth, and prints `NEOHIVE_HIVE=<id>`. Idempotent — first instance of a
SHARED repo indexes it, the rest reuse it.

Mirror/transparency. NeoHive's git sync indexes a remote BRANCH tip; the hosted server
can't reach a local file:// mirror. We publish a public mirror NeoHiveAI/swebench-mirror-
<repo> with a branch pinned at the indexed commit (`swebench-base` for SHARED,
`swebench/<instance_id>` for PER-INSTANCE) and point NeoHive at it. Branch tip SHA ==
pilot_repo_versions.json's indexed_base_commit == an entry in pilot_subset.json — a
content-addressed proof of exactly what was indexed.

Auth (hosted): Cloudflare Access service token + project id. Set:
  NEOHIVE_BASE (default https://neohive.logilica.com), NEOHIVE_PROJECT,
  NEOHIVE_CF_ACCESS_CLIENT_ID, NEOHIVE_CF_ACCESS_CLIENT_SECRET.
GitHub (indexing host only): pushes via the `github-work` SSH alias; repo create via `gh`.
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

HERE = Path(__file__).resolve().parent
DATASET = "princeton-nlp/SWE-Bench_Verified"
SPLIT = "test"
MIRROR_ORG = "NeoHiveAI"
SSH_HOST = "github-work"  # ~/.ssh/config alias for the logilica GitHub account
BASE_BRANCH = "swebench-base"       # SHARED repos: one branch at the oldest-used commit
SYNTH_BRANCH_PREFIX = "swebench"    # PER-INSTANCE repos: swebench/<instance_id> per branch
# Code-specialized embedding that fits the dev box (the 3584d nomic-embed-code
# variants "Won't fit"). Pinning a code model is what makes Arm B retrieve source
# instead of docs — see Knowledge memory #302.
DEFAULT_EMBEDDING_MODEL = os.environ.get("NEOHIVE_EMBEDDING_MODEL", "google/embeddinggemma-300m-code")
# Defense-in-depth on top of the structural ancestry guard.
DEFAULT_TEST_BLOCKLIST = ["**/test/**", "**/tests/**", "**/testing/**", "**/*_test.py", "**/test_*.py"]


def run(cmd: list[str], cwd: str | None = None, check: bool = True, input_text: str | None = None):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True, input=input_text)


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def mirror_name(repo: str) -> str:
    return f"swebench-mirror-{repo.split('/')[1]}"


def load_instance(instance_id: str) -> dict:
    """Full HF dataset row (repo, base_commit, problem_statement, ...). Heavy; used by
    run_opencode.py for the task prompt. index_instance's own path avoids HF and reads
    routing from pilot_repo_versions.json instead."""
    from datasets import load_dataset  # heavy; host-side only

    ds = load_dataset(DATASET, split=SPLIT)
    rows = ds.filter(lambda r: r["instance_id"] == instance_id)
    if len(rows) == 0:
        raise SystemExit(f"instance not found in {DATASET}:{SPLIT}: {instance_id}")
    return rows[0]


# ---- per-instance routing plan (from the precomputed, committed artifact) ----

def load_repo_versions(path: str = "pilot_repo_versions.json") -> list[dict]:
    p = Path(path)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        raise SystemExit(f"{p} missing — run compute_repo_versions.py first")
    return json.loads(p.read_text())["repos"]


def resolve_plan(instance_id: str) -> dict:
    """Resolve how to index/route this instance from pilot_repo_versions.json.

    Returns: repo, mode, indexed_commit, hive_name, branch, and guard_commits — the
    base_commits the indexed commit must be an ancestor of (all of the repo's instances
    for SHARED; just this instance for PER-INSTANCE)."""
    for rec in load_repo_versions():
        row = next((r for r in rec["instances"] if r["instance_id"] == instance_id), None)
        if not row:
            continue
        guard = ([r["base_commit"] for r in rec["instances"]] if rec["mode"] == "shared"
                 else [row["base_commit"]])
        return {
            "repo": rec["repo"],
            "mode": rec["mode"],
            "indexed_commit": row["indexed_base_commit"],
            "hive_name": row["hive_name"],
            "branch": row["branch"],
            "guard_commits": guard,
        }
    raise SystemExit(f"{instance_id} not in pilot_repo_versions.json — run compute_repo_versions.py")


# ---- git cache / mirror (indexing host only) ----

def ensure_repo_cache(repo: str, commits: list[str], cache: Path) -> Path:
    """Full clone of the upstream repo (cached per upstream; a full clone is required
    because GitHub rejects shallow pushes). Ensures every commit in `commits` is present
    (fetch by SHA if an older cache lacks it)."""
    url = f"https://github.com/{repo}.git"
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            run(["rm", "-rf", str(cache)])
        print(f"[index] full-cloning {url} (cached per upstream) ...")
        run(["git", "clone", url, str(cache)])
    for c in commits:
        if run(["git", "-C", str(cache), "cat-file", "-e", c], check=False).returncode != 0:
            run(["git", "-C", str(cache), "fetch", "origin", c], check=False)
    return cache


def assert_ancestry(cache: Path, indexed: str, commits: list[str]) -> None:
    """Re-prove leakage-safety on the indexing host: the indexed commit must be an
    ancestor of every routed base_commit (so the indexed tree predates every fix +
    tests). For PER-INSTANCE this is the self-check indexed==base."""
    for c in commits:
        if run(["git", "-C", str(cache), "merge-base", "--is-ancestor", indexed, c], check=False).returncode != 0:
            raise SystemExit(f"[guard] {indexed[:12]} is NOT an ancestor of {c[:12]} — leakage risk, aborting")
    print(f"[guard] PASS — {indexed[:12]} is an ancestor of all {len(commits)} routed base_commit(s).")


def make_branch(cache: Path, branch: str, commit: str) -> str:
    run(["git", "-C", str(cache), "branch", "-f", branch, commit])
    head = run(["git", "-C", str(cache), "rev-parse", branch]).stdout.strip()
    assert head == commit, f"branch {branch} at {head} != {commit}"
    return branch


def ensure_mirror(repo: str) -> tuple[str, str]:
    """Ensure the public mirror repo exists under NeoHiveAI. Returns (ssh_url, https_url)."""
    name = mirror_name(repo)
    full = f"{MIRROR_ORG}/{name}"
    if run(["gh", "repo", "view", full], check=False).returncode != 0:
        print(f"[index] creating public mirror {full} ...")
        run(["gh", "repo", "create", full, "--public",
             "--description", f"SWE-bench Arm-B mirror of {repo}. Each branch is the repo at an "
             f"indexed commit (an ancestor of the routed instance's base_commit, pre-fix) — the "
             f"exact tree fed to NeoHive. Verify branch SHAs against pilot_repo_versions.json."])
    return f"git@{SSH_HOST}:{full}.git", f"https://github.com/{full}.git"


def publish_branch(cache: Path, ssh_url: str, branch: str) -> None:
    print(f"[index] pushing {branch} -> {ssh_url}")
    run(["git", "-C", str(cache), "push", "-f", ssh_url, branch])
    remote = run(["git", "-C", str(cache), "ls-remote", ssh_url, f"refs/heads/{branch}"]).stdout.strip()
    assert remote, f"branch {branch} not found on mirror after push"
    print(f"[index] mirror ref: {remote.split()[0][:12]}")


# ---- NeoHive ingestion (hosted; Cloudflare Access) ----

def _neohive_req(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ.get("NEOHIVE_BASE", "https://neohive.logilica.com").rstrip("/")
    project = os.environ["NEOHIVE_PROJECT"]
    headers = {
        "Content-Type": "application/json",
        "X-HiveMind-Id": project,
        # CF WAF blocks the default Python-urllib UA (Error 1010); any UA passes.
        "User-Agent": "neohive-agent-bench/0.1",
    }
    cf_id = os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("NEOHIVE_CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    if os.environ.get("NEOHIVE_PAT"):
        headers["Authorization"] = f"Bearer {os.environ['NEOHIVE_PAT']}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def find_indexed_hive(hive_name: str, indexed: str) -> str | None:
    """Return the id of the hive named `hive_name` if it exists AND is already indexed at
    `indexed`, else None. Short-circuits before any clone/mirror/push — so a host without
    gh/GitHub SSH (the x86 sweep box) reuses hives pre-indexed elsewhere, over CF only."""
    hives = _neohive_req("GET", "/api/hives")
    existing = next((h for h in hives.get("items", []) if h.get("name") == hive_name and h.get("type") == "repo"), None)
    if not existing:
        return None
    cfgs = _neohive_req("GET", f"/api/hives/{existing['id']}/sync-configs").get("items", [])
    return existing["id"] if any(c.get("last_indexed_sha") == indexed for c in cfgs) else None


def _teardown_hive(hive_id: str) -> None:
    """Delete a hive SAFELY: cancel + delete its sync-configs FIRST, then the hive.

    Deleting a hive while a git-sync is in flight leaves a zombie sync that keeps
    reindexing files against a now-missing client, and its recurring schedule survives
    to re-spawn on restart (observed 2026-07-01: one such zombie starved the in-process
    sync queue for hours). Cancelling + deleting the sync-config first aborts any running
    sync and removes the schedule (removeByTenancy) before the hive goes."""
    for c in _neohive_req("GET", f"/api/hives/{hive_id}/sync-configs").get("items", []):
        try:
            _neohive_req("POST", f"/api/hives/{hive_id}/sync-configs/{c['id']}/cancel")
        except urllib.error.HTTPError:
            pass  # 409 = nothing running; fine
        _neohive_req("DELETE", f"/api/hives/{hive_id}/sync-configs/{c['id']}")
    _neohive_req("DELETE", f"/api/hives/{hive_id}")


def index_into_neohive(repo: str, hive_name: str, https_url: str, branch: str, indexed: str,
                       mode: str, poll_timeout: int) -> str:
    # Idempotent: reuse an already-indexed hive; else delete a stale one and recreate.
    hid = find_indexed_hive(hive_name, indexed)
    if hid:
        print(f"[index] reusing hive {hid} (already indexed at {indexed[:12]})")
        return hid
    hives = _neohive_req("GET", "/api/hives")
    stale = next((h for h in hives.get("items", []) if h.get("name") == hive_name and h.get("type") == "repo"), None)
    if stale:
        _teardown_hive(stale["id"])  # exists but not at indexed commit -> tear down safely, recreate
        print(f"[index] tore down stale hive {stale['id']} (not at indexed commit); recreating")

    hive = _neohive_req("POST", "/api/hives", {
        "name": hive_name,
        "type": "repo",
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "description": f"SWE-bench Arm B ({mode}): {repo} @ {indexed[:12]} (ancestor of routed base_commit(s))",
    })
    hive_id = hive["id"]
    print(f"[index] created hive {hive_id} name={hive_name} (model={DEFAULT_EMBEDDING_MODEL})")

    cfg = _neohive_req("POST", f"/api/hives/{hive_id}/sync-configs", {
        "repo_url": https_url,
        "branch": branch,
        "file_blocklist": DEFAULT_TEST_BLOCKLIST,
    })
    cfg_id = cfg["id"]
    print(f"[index] sync-config {cfg_id} (repo={https_url}, branch={branch}); polling (GET-only)...")

    # POST sync-config auto-dispatches the initial sync. Do NOT re-trigger — extra
    # triggers just re-queue redundant syncs. First sync also downloads + warms the
    # embedding model, so be patient.
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        cur = _neohive_req("GET", f"/api/hives/{hive_id}/sync-configs/{cfg_id}")
        if cur.get("error_message"):
            raise SystemExit(f"[index] sync error for {repo}: {cur['error_message']}")
        sha = cur.get("last_indexed_sha")
        if sha:
            assert sha == indexed, f"indexed {sha} != planned {indexed}"
            print(f"[index] READY — hive {hive_id} indexed at {sha[:12]} == planned commit")
            return hive_id
        time.sleep(20)
    raise SystemExit(f"[index] sync not ready within {poll_timeout}s (hive {hive_id})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Index a SWE-bench instance's repo into NeoHive per its routing plan (Arm B).")
    ap.add_argument("instance_id", help="Pilot instance; its plan (shared/per-instance) is resolved from pilot_repo_versions.json.")
    ap.add_argument("--workdir", default=os.environ.get("ARMB_WORKDIR", ".localdata/armb"))
    ap.add_argument("--guard-only", action="store_true", help="Clone + ancestry guard only (no publish, no NeoHive).")
    ap.add_argument("--publish-only", action="store_true", help="Clone + guard + mirror push, then STOP (no NeoHive). Prints MIRROR_URL/MIRROR_BRANCH for manual hive creation.")
    ap.add_argument("--poll-timeout", type=int, default=900, help="Seconds to wait for indexing (cold model warmup is slow).")
    args = ap.parse_args()

    plan = resolve_plan(args.instance_id)
    repo, mode, indexed = plan["repo"], plan["mode"], plan["indexed_commit"]
    hive_name, branch = plan["hive_name"], plan["branch"]
    print(f"[index] {args.instance_id} -> {repo} [{mode}]; hive={hive_name} indexed={indexed[:12]}")

    # Short-circuit: if the target hive is already indexed at the planned commit, reuse it
    # without cloning/mirroring/pushing (so a gh/SSH-less host can run off pre-indexed
    # hives via CF alone). Indexing hosts (with gh + GitHub SSH) create them; run hosts reuse.
    if not args.guard_only and not args.publish_only:
        hid = find_indexed_hive(hive_name, indexed)
        if hid:
            print(f"[index] reusing already-indexed hive {hid} for {repo} (skip clone/mirror/publish)")
            print(f"NEOHIVE_HIVE={hid}")
            return 0

    cache = Path(args.workdir) / "repos" / repo_slug(repo)
    ensure_repo_cache(repo, [indexed] + plan["guard_commits"], cache)
    assert_ancestry(cache, indexed, plan["guard_commits"])  # defense-in-depth over the precomputed proof
    make_branch(cache, branch, indexed)

    if args.guard_only:
        print(f"[index] guard-only OK for {repo} [{mode}] (indexed {indexed[:12]}).")
        return 0

    ssh_url, https_url = ensure_mirror(repo)
    publish_branch(cache, ssh_url, branch)

    if args.publish_only:
        print(f"MIRROR_URL={https_url}")
        print(f"MIRROR_BRANCH={branch}")
        print(f"HIVE_NAME={hive_name}")
        return 0

    hive_id = index_into_neohive(repo, hive_name, https_url, branch, indexed, mode, args.poll_timeout)
    print(f"NEOHIVE_HIVE={hive_id}")  # consumed by run_opencode.py
    return 0


if __name__ == "__main__":
    sys.exit(main())
