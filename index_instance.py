#!/usr/bin/env python3
"""
HIVE-268 — Index one SWE-bench instance's repo @ base_commit into NeoHive (Arm B).

Validated end-to-end against the hosted NeoHive (neohive.logilica.com) on
2026-06-30 for psf__requests-1142.

Contamination guard is STRUCTURAL: both the gold `patch` (the fix) and the
`test_patch` (the tests that check it) are diffs applied ON TOP of base_commit, so
the pristine base_commit tree contains neither. We index exactly at base_commit and
assert each patch applies forward (we are pre-fix) but not in reverse (answer absent).

NeoHive's git sync indexes a remote BRANCH tip (`cloneRepo(url, branch)` does
`git clone --branch <ref> --depth 1`), and the hosted server can't reach a local
`file://` mirror. So we publish a public mirror under NeoHiveAI with a branch
`swebench/<instance_id>` pinned at base_commit, and point NeoHive at it. The mirror
doubles as the open-source TRANSPARENCY artifact: the bench repo embeds it as a
submodule pinned at base_commit, and anyone can verify the branch SHA == the
dataset's base_commit (git content-addressing does the rest).

Pipeline (per instance):
  1. Load the instance row (repo, base_commit, patch, test_patch) from the dataset.
  2. Clone/refresh a per-upstream cache; create branch swebench/<id> at base_commit.
  3. CONTAMINATION GUARD (no NeoHive needed).
  4. Publish: ensure the NeoHiveAI/swebench-mirror-<repo> mirror exists; push the branch.
  5. NeoHive: create a per-instance code-embedding `repo` hive; create+trigger a
     sync-config pointed at the mirror branch; poll until indexed. Print the hive id.

Auth (hosted): Cloudflare Access service token + project id. Set:
  NEOHIVE_BASE (default https://neohive.logilica.com), NEOHIVE_PROJECT,
  NEOHIVE_CF_ACCESS_CLIENT_ID, NEOHIVE_CF_ACCESS_CLIENT_SECRET.
GitHub: pushes via the `github-work` SSH alias; repo create/exists via `gh`.
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
MIRROR_ORG = "NeoHiveAI"
SSH_HOST = "github-work"  # ~/.ssh/config alias for the logilica GitHub account
SYNTH_BRANCH_PREFIX = "swebench"
# Code-specialized embedding that fits the dev box (the 3584d nomic-embed-code
# variants "Won't fit"). Pinning a code model is what makes Arm B retrieve source
# instead of docs — see Knowledge memory #302.
DEFAULT_EMBEDDING_MODEL = os.environ.get("NEOHIVE_EMBEDDING_MODEL", "google/embeddinggemma-300m-code")
# Defense-in-depth on top of the structural guard.
DEFAULT_TEST_BLOCKLIST = ["**/test/**", "**/tests/**", "**/testing/**", "**/*_test.py", "**/test_*.py"]


def run(cmd: list[str], cwd: str | None = None, check: bool = True, input_text: str | None = None):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True, input=input_text)


def load_instance(instance_id: str) -> dict:
    from datasets import load_dataset  # heavy; host-side only

    ds = load_dataset(DATASET, split=SPLIT)
    rows = ds.filter(lambda r: r["instance_id"] == instance_id)
    if len(rows) == 0:
        raise SystemExit(f"instance not found in {DATASET}:{SPLIT}: {instance_id}")
    return rows[0]


def mirror_name(repo: str) -> str:
    return f"swebench-mirror-{repo.split('/')[1]}"


def ensure_repo_cache(repo: str, base_commit: str, cache: Path) -> Path:
    """Full clone of the upstream repo (cached per upstream; needed to push), with
    base_commit present. A full clone is required because GitHub rejects shallow
    pushes; subsequent instances of the same repo reuse the cache and just add a branch."""
    url = f"https://github.com/{repo}.git"
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            run(["rm", "-rf", str(cache)])
        print(f"[index] full-cloning {url} (cached per upstream) ...")
        run(["git", "clone", url, str(cache)])
    # Make sure base_commit is present (fetch by SHA if an older cache lacks it).
    if run(["git", "-C", str(cache), "cat-file", "-e", base_commit], check=False).returncode != 0:
        run(["git", "-C", str(cache), "fetch", "origin", base_commit], check=False)
    return cache


def make_branch(cache: Path, instance_id: str, base_commit: str) -> str:
    branch = f"{SYNTH_BRANCH_PREFIX}/{instance_id}"
    run(["git", "-C", str(cache), "branch", "-f", branch, base_commit])
    head = run(["git", "-C", str(cache), "rev-parse", branch]).stdout.strip()
    assert head == base_commit, f"branch {branch} at {head} != base_commit {base_commit}"
    return branch


def _applies(cache: Path, patch_text: str, reverse: bool) -> bool:
    if not patch_text.strip():
        return False
    args = ["git", "-C", str(cache), "apply", "--check"] + (["--reverse"] if reverse else []) + ["-"]
    return subprocess.run(args, input=patch_text, text=True, capture_output=True).returncode == 0


def contamination_guard(cache: Path, instance_id: str, base_commit: str, gold: str, test: str) -> None:
    # Check against the base_commit tree (worktree may be on another branch; use the index-free apply at that tree).
    run(["git", "-C", str(cache), "checkout", "--quiet", base_commit])
    failures = []
    for label, patch in [("gold patch", gold), ("test_patch", test)]:
        fwd, rev = _applies(cache, patch, False), _applies(cache, patch, True)
        ok = fwd and not rev
        print(f"[guard] {label:11} applies_forward={fwd} already_present={rev} -> {'OK' if ok else 'CONTAMINATED'}")
        if not ok:
            failures.append(f"{label} (forward={fwd}, reverse={rev})")
    if failures:
        raise SystemExit(f"[guard] CONTAMINATION GUARD FAILED for {instance_id}: {', '.join(failures)}")
    print("[guard] PASS — base_commit tree contains neither the fix nor its tests.")


def ensure_mirror(repo: str) -> tuple[str, str]:
    """Ensure the public mirror repo exists under NeoHiveAI. Returns (ssh_url, https_url)."""
    name = mirror_name(repo)
    full = f"{MIRROR_ORG}/{name}"
    if run(["gh", "repo", "view", full], check=False).returncode != 0:
        print(f"[index] creating public mirror {full} ...")
        run(["gh", "repo", "create", full, "--public",
             "--description", f"SWE-bench Arm-B mirror of {repo}. Branch swebench/<instance_id> = the "
             f"repo at that instance's base_commit (pre-fix) — the exact tree indexed into NeoHive. "
             f"Verify the branch SHA equals the dataset base_commit."])
    return f"git@{SSH_HOST}:{full}.git", f"https://github.com/{full}.git"


def publish_branch(cache: Path, ssh_url: str, branch: str) -> None:
    print(f"[index] pushing {branch} -> {ssh_url}")
    run(["git", "-C", str(cache), "push", ssh_url, branch])
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


def find_indexed_hive(instance_id: str, base_commit: str) -> str | None:
    """Return the id of an existing repo hive for this instance already indexed at
    base_commit, else None. Used to SHORT-CIRCUIT before any clone/mirror/push — so a
    host without gh/GitHub-SSH (e.g. the x86 sweep box) can reuse hives pre-indexed
    elsewhere, reaching hosted NeoHive over CF only."""
    hives = _neohive_req("GET", "/api/hives")
    existing = next((h for h in hives.get("items", []) if h.get("name") == instance_id and h.get("type") == "repo"), None)
    if not existing:
        return None
    cfgs = _neohive_req("GET", f"/api/hives/{existing['id']}/sync-configs").get("items", [])
    return existing["id"] if any(c.get("last_indexed_sha") == base_commit for c in cfgs) else None


def index_into_neohive(instance_id: str, repo: str, https_url: str, branch: str, base_commit: str,
                       poll_timeout: int) -> str:
    # Idempotent: reuse an already-indexed hive; else delete a stale one and recreate.
    hid = find_indexed_hive(instance_id, base_commit)
    if hid:
        print(f"[index] reusing hive {hid} (already indexed at {base_commit[:12]})")
        return hid
    hives = _neohive_req("GET", "/api/hives")
    stale = next((h for h in hives.get("items", []) if h.get("name") == instance_id and h.get("type") == "repo"), None)
    if stale:
        _neohive_req("DELETE", f"/api/hives/{stale['id']}")  # exists but stale -> recreate
        print(f"[index] deleted stale hive {stale['id']} (not at base_commit); recreating")

    hive = _neohive_req("POST", "/api/hives", {
        "name": instance_id,
        "type": "repo",
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "description": f"SWE-bench Arm B: {repo} @ {base_commit[:12]} (instance {instance_id})",
    })
    hive_id = hive["id"]
    print(f"[index] created hive {hive_id} (model={DEFAULT_EMBEDDING_MODEL})")

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
            raise SystemExit(f"[index] sync error for {instance_id}: {cur['error_message']}")
        sha = cur.get("last_indexed_sha")
        if sha:
            assert sha == base_commit, f"indexed {sha} != base_commit {base_commit}"
            print(f"[index] READY — hive {hive_id} indexed at {sha[:12]} == base_commit")
            return hive_id
        time.sleep(20)
    raise SystemExit(f"[index] sync not ready within {poll_timeout}s (hive {hive_id})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Index one SWE-bench instance @ base_commit into NeoHive (Arm B).")
    ap.add_argument("instance_id")
    ap.add_argument("--workdir", default=os.environ.get("ARMB_WORKDIR", ".localdata/armb"))
    ap.add_argument("--guard-only", action="store_true", help="Clone + contamination guard only (no publish, no NeoHive).")
    ap.add_argument("--poll-timeout", type=int, default=900, help="Seconds to wait for indexing (cold model warmup is slow).")
    args = ap.parse_args()

    inst = load_instance(args.instance_id)
    repo, base_commit = inst["repo"], inst["base_commit"]
    cache = Path(args.workdir) / "repos" / repo.replace("/", "__")

    # Short-circuit: if this instance's hive is already indexed on NeoHive, reuse it
    # without cloning/mirroring/pushing (so a gh/SSH-less host can run off pre-indexed
    # hives via CF alone). Indexing hosts (with gh + GitHub SSH) create them; run hosts reuse.
    if not args.guard_only:
        hid = find_indexed_hive(args.instance_id, base_commit)
        if hid:
            print(f"[index] reusing already-indexed hive {hid} (skip clone/mirror/publish)")
            print(f"NEOHIVE_HIVE={hid}")
            return 0

    ensure_repo_cache(repo, base_commit, cache)
    branch = make_branch(cache, args.instance_id, base_commit)
    contamination_guard(cache, args.instance_id, base_commit, inst.get("patch", ""), inst.get("test_patch", ""))

    if args.guard_only:
        print(f"[index] guard-only OK for {args.instance_id}.")
        return 0

    ssh_url, https_url = ensure_mirror(repo)
    publish_branch(cache, ssh_url, branch)
    hive_id = index_into_neohive(args.instance_id, repo, https_url, branch, base_commit, args.poll_timeout)
    print(f"NEOHIVE_HIVE={hive_id}")  # consumed by run_arm_b.sh
    return 0


if __name__ == "__main__":
    sys.exit(main())
