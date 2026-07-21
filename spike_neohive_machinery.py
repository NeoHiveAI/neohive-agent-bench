#!/usr/bin/env python3
"""Spike check (a), NeoHive-side machinery — runnable WITHOUT Docker/opencode.

Proves, against a LOCAL NeoHive, that the compounding experience pool:
  1. persists as one per-career project + Knowledge hive,
  2. GROWS as filtered learnings are written (MCP memory_store),
  3. can be FS-snapshotted (byte-exact) after a round, and
  4. can be rolled back to an earlier dose (REST soft-delete of post-snapshot adds).

This is the part of spike check (a) that does NOT need the agent-in-container (which
requires Docker + OPENROUTER_API_KEY + SWE-bench images, i.e. the x86 host). The
agent-driven growth is exercised by run_rounds.py on that host; here we drive the
writes directly to validate the plumbing.

Isolated + self-cleaning: creates a throwaway project (zzz-...), does its work, then
deletes it. Pass --keep to leave it for inspection.

Env: NEOHIVE_BASE (default http://127.0.0.1:3577), NEOHIVE_DATA_DIR (the local data
dir holding hiveminds/<project>/hives/...).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import hive_snapshot as snap
from neohive_rest import NeoHiveClient, NeoHiveError

DEFAULT_DATA_DIR = "/Users/naderawad/Logilica/MemVec/server/.localdata"

NOTES = [
    "In django, timezone regressions usually come from naive datetimes reaching the ORM; "
    "make values aware early rather than at comparison time.",
    "When a SWE-bench django task touches DateTimeField coercion, check DateField vs "
    "DateTimeField pre_save/clean divergence before editing.",
    "Test failures that mention assertWarns often want a RuntimeWarning raised, not an "
    "exception; the fix is usually a warnings.warn call guarded by a settings flag.",
    "For migration-related django issues, reproduce with makemigrations --check before "
    "and after the edit to confirm the schema delta is intended.",
]


def _find_knowledge_hive(client: NeoHiveClient) -> dict:
    hives = client.list_hives()
    kh = next((h for h in hives if h.get("type") == "knowledge"), None)
    if not kh:
        raise SystemExit(f"no Knowledge hive in project {client.project}; hives={hives}")
    return kh


def _wait_count(client: NeoHiveClient, hive_id: str, want: int, tries: int = 15) -> int:
    """memory_store embeds before returning; still poll a little for eventual visibility."""
    n = -1
    for _ in range(tries):
        n = client.memory_count(hive_id)
        if n >= want:
            return n
        time.sleep(1.0)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not delete the throwaway project")
    ap.add_argument("--data-dir", default=os.environ.get("NEOHIVE_DATA_DIR", DEFAULT_DATA_DIR))
    args = ap.parse_args()
    base = os.environ.get("NEOHIVE_BASE", "http://127.0.0.1:3577").rstrip("/")

    admin = NeoHiveClient(base=base)
    name = f"zzz-multirun-smoke-{int(time.time())}"
    proj = admin.create_project(name, description="throwaway; multi-run spike; safe to delete")
    pid = proj.get("id")
    print(f"[spike-a] created isolated project {pid} ({name})")
    ok = True
    snapdir = Path(tempfile.mkdtemp(prefix="neohive-snap-"))
    try:
        client = NeoHiveClient(base=base, project=pid)
        kh = _find_knowledge_hive(client)
        khid = kh["id"]
        hdir = snap.hive_dir(args.data_dir, pid, khid)
        print(f"[spike-a] Knowledge hive {khid}; on-disk {hdir}")

        # (1)+(2) pool starts empty, grows as we write two learnings (round 0)
        start = client.memory_count(khid)
        for note in NOTES[:2]:
            client.store_memory(note, "insight", importance=6, tags=["django", "swebench"])
        after_r0 = _wait_count(client, khid, start + 2)
        grew = after_r0 - start
        print(f"[spike-a] round0 writes: count {start} -> {after_r0} (grew {grew})")
        ok &= grew == 2

        # (3) FS snapshot after round 0 (byte-exact) + REST snapshot for live rollback
        fs_manifest = snap.snapshot_fs(hdir, snapdir / "round0") if hdir.is_dir() else None
        rest_snap = snap.snapshot_rest(client, khid, round_index=0)
        if fs_manifest:
            print(f"[spike-a] FS snapshot round0: {fs_manifest['files']} ({fs_manifest['bytes']} bytes)")
        else:
            print(f"[spike-a] FS snapshot skipped (hive dir not found at {hdir})")
        print(f"[spike-a] REST snapshot round0: {rest_snap.count} memories {rest_snap.by_type}")

        # round 1 appends two more learnings -> pool keeps growing
        for note in NOTES[2:4]:
            client.store_memory(note, "insight", importance=6, tags=["django", "swebench"])
        after_r1 = _wait_count(client, khid, after_r0 + 2)
        print(f"[spike-a] round1 writes: count {after_r0} -> {after_r1} (grew {after_r1 - after_r0})")
        ok &= after_r1 == after_r0 + 2

        # (4) roll back to the round-0 dose (live, no restart)
        report = snap.restore_rest(client, khid, rest_snap)
        after_restore = client.memory_count(khid)
        print(f"[spike-a] rollback to round0: {report}; count now {after_restore}")
        ok &= after_restore == after_r0 and report["unrestorable_removed"] == 0

        # FS snapshot is a portable, valid single-file db
        if fs_manifest:
            import sqlite3
            db = snapdir / "round0" / "cognitive-memory.db"
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            tbls = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            con.close()
            print(f"[spike-a] FS snapshot db is readable; {len(tbls)} tables incl 'memories'={'memories' in tbls}")
            ok &= not (snapdir / "round0" / "cognitive-memory.db-wal").exists()

        print(f"\n[spike-a] RESULT: {'PASS' if ok else 'FAIL'} — "
              f"pool persisted, grew {after_r1 - start} across 2 rounds, snapshot+rollback exact")
    finally:
        if args.keep:
            print(f"[spike-a] --keep set; leaving project {pid} and snapshots at {snapdir}")
        else:
            try:
                admin.delete_project(pid)
                print(f"[spike-a] cleaned up project {pid}")
            except NeoHiveError as e:
                print(f"[spike-a] WARNING: cleanup failed for {pid}: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
