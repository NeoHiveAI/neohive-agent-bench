#!/usr/bin/env python3
"""HIVE-334 — persistent per-repo hive lifecycle + per-round snapshot/restore.

The compounding run keeps one experience-pool hive alive for a whole repo career
(never wiped between instances — that is the old single-pass behaviour we are moving
away from) and snapshots it after each round so any *dose* of accumulated memory can
be restored and a round re-run reproducibly.

The NeoHive server exposes **no native snapshot/restore** (its `memory_snapshots`
table is daily count telemetry only). Because the compounding run targets a **local**
NeoHive whose hive data dir is on local disk, we snapshot at two fidelities:

  * **FS snapshot (byte-exact, primary):** copy the hive's on-disk state — the
    `cognitive-memory.db` (via SQLite's online-backup API, which folds the WAL into a
    consistent single-file copy while the server keeps running) plus the co-located
    `vectors.lance/` embedding store. This is the audit / reproduction artifact and
    the only thing that faithfully preserves embeddings, ids and access-stats.
    Restoring it needs the server to *reopen* the hive (stop/restart or archive
    toggle), since it holds the DB fd open.

  * **REST soft-delete rollback (live, no restart):** the experience pool is
    append-only between rounds, so rolling back to dose-K == deactivating exactly the
    memories added after round K's snapshot (`DELETE /api/hives/:id/memories/:mid`).
    Byte-exact for the surviving memories; used for live between-round dose control.

Stdlib only.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

HIVE_DB = "cognitive-memory.db"
_WAL_SIDECARS = ("cognitive-memory.db-wal", "cognitive-memory.db-shm")


# --------------------------------------------------------------------------- #
# Locating a hive's on-disk directory (local instance)
# --------------------------------------------------------------------------- #

def hive_dir(data_dir: str | Path, project_id: str, hive_id: str) -> Path:
    """Path to a hive's data dir under a local NeoHive data dir, e.g.
    ``<data_dir>/hiveminds/<project_id>/hives/<hive_id>/``."""
    return Path(data_dir) / "hiveminds" / project_id / "hives" / hive_id


# --------------------------------------------------------------------------- #
# FS snapshot / restore (byte-exact; local instance)
# --------------------------------------------------------------------------- #

def snapshot_fs(hdir: str | Path, dest: str | Path, *, live: bool = True) -> dict:
    """Copy hive dir `hdir` into `dest` as a round snapshot. When `live` (server may
    have the DB open), the `.db` is captured via SQLite online backup so the copy is
    transaction-consistent without a WAL/SHM sidecar; other files/dirs (notably
    `vectors.lance/`) are copied as-is. Returns a manifest dict (also written to
    `dest/manifest.json`)."""
    hdir = Path(hdir)
    dest = Path(dest)
    if not hdir.is_dir():
        raise FileNotFoundError(f"hive dir not found: {hdir}")
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    db = hdir / HIVE_DB
    if db.exists():
        if live:
            _sqlite_online_backup(db, dest / HIVE_DB)
        else:
            shutil.copy2(db, dest / HIVE_DB)
        copied.append(HIVE_DB)

    for child in sorted(hdir.iterdir()):
        if child.name == HIVE_DB:
            continue
        # when live, the WAL/SHM were already folded into the .db backup — skip them
        # so a restore can't replay a stale WAL over the restored db.
        if live and child.name in _WAL_SIDECARS:
            continue
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)
        copied.append(child.name)

    manifest = {
        "kind": "fs",
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": str(hdir),
        "live": live,
        "files": copied,
        "bytes": _tree_bytes(dest),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def restore_fs(snapshot_dir: str | Path, hdir: str | Path) -> dict:
    """Replace hive dir `hdir` with the contents of `snapshot_dir` (byte-exact
    restore). The caller MUST ensure the server has released the hive (stop the
    instance or archive the project first); this function only swaps files. Stale
    WAL/SHM are removed first so SQLite can't replay them over the restored db."""
    snapshot_dir = Path(snapshot_dir)
    hdir = Path(hdir)
    hdir.mkdir(parents=True, exist_ok=True)

    for sidecar in _WAL_SIDECARS:
        (hdir / sidecar).unlink(missing_ok=True)

    restored: list[str] = []
    for child in sorted(snapshot_dir.iterdir()):
        if child.name == "manifest.json":
            continue
        target = hdir / child.name
        if child.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
        restored.append(child.name)
    return {"restored_into": str(hdir), "files": restored}


def _sqlite_online_backup(src_db: Path, dest_db: Path) -> None:
    """Transaction-consistent copy of a (possibly live, WAL-mode) SQLite db into a
    single portable file. The backup API copies the source's WAL-mode header flag, so
    we flip the snapshot to rollback-journal mode afterwards — that checkpoints any
    copied WAL frames into the main file and leaves no ``-wal``/``-shm`` sidecar."""
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(dest_db))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # -> clean single-file snapshot
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _tree_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# REST-level snapshot / rollback (live; append-only dose control)
# --------------------------------------------------------------------------- #

@dataclass
class MemorySnapshot:
    """A round's memory-set snapshot, taken over REST (content export). Serializable
    to JSON for the per-round audit trail."""
    round_index: int
    hive_id: str
    taken_at: str
    count: int
    memory_ids: list = field(default_factory=list)
    by_type: dict = field(default_factory=dict)
    memories: list = field(default_factory=list)  # full rows (content, type, importance, ...)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MemorySnapshot":
        return cls(**json.loads(text))


@dataclass
class RestorePlan:
    to_delete: list        # active now but not in the target snapshot -> deactivate
    to_readd: list         # in the snapshot but gone now -> cannot re-add over REST


def plan_restore(current_ids, snapshot_ids) -> RestorePlan:
    """Diff for an append-only rollback: memories added since the snapshot are removed;
    memories that vanished can't be re-created over REST (reported, not silently lost)."""
    cur = set(current_ids)
    snap = set(snapshot_ids)
    return RestorePlan(
        to_delete=sorted(cur - snap, key=str),
        to_readd=sorted(snap - cur, key=str),
    )


def snapshot_rest(client, hive_id: str, round_index: int) -> MemorySnapshot:
    """Export a hive's active memory set via REST (content, no embeddings)."""
    mems = client.get_memories(hive_id)
    ids = [m.get("id") for m in mems]
    by_type = Counter(m.get("type", "unknown") for m in mems)
    return MemorySnapshot(
        round_index=round_index,
        hive_id=hive_id,
        taken_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        count=len(mems),
        memory_ids=ids,
        by_type=dict(by_type),
        memories=mems,
    )


def restore_rest(client, hive_id: str, snapshot: MemorySnapshot) -> dict:
    """Roll a hive back to `snapshot` by deactivating every memory added since. Works
    on a running server (no restart). Returns a report; `unrestorable_removed` > 0
    means some snapshot memories had been deleted and cannot be re-created over REST."""
    current = [m.get("id") for m in client.get_memories(hive_id)]
    plan = plan_restore(current, snapshot.memory_ids)
    for mid in plan.to_delete:
        client.delete_memory(hive_id, mid)
    return {
        "hive_id": hive_id,
        "restored_to_round": snapshot.round_index,
        "deleted": len(plan.to_delete),
        "unrestorable_removed": len(plan.to_readd),
    }
