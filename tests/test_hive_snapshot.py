"""HIVE-334 — tests for hive snapshot/restore (FS byte-exact + REST rollback).

Offline: the FS tests build a synthetic hive dir (a real SQLite db in WAL mode plus a
fake ``vectors.lance/`` dir) and prove snapshot -> mutate -> restore round-trips; the
REST tests use a fake client to prove append-only rollback removes only post-snapshot
memories. Stdlib only.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hive_snapshot import (
    MemorySnapshot,
    RestorePlan,
    hive_dir,
    plan_restore,
    restore_fs,
    restore_rest,
    snapshot_fs,
    snapshot_rest,
)


def _make_hive_dir(root: Path, rows: int) -> Path:
    """A synthetic hive dir: cognitive-memory.db (WAL, `rows` memories) + vectors.lance/."""
    hdir = root
    hdir.mkdir(parents=True, exist_ok=True)
    db = hdir / "cognitive-memory.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    con.executemany("INSERT INTO memories (content) VALUES (?)", [(f"m{i}",) for i in range(rows)])
    con.commit()
    con.close()
    lance = hdir / "vectors.lance"
    lance.mkdir(exist_ok=True)
    (lance / "data.manifest").write_text("v1")
    return hdir


def _row_count(db: Path) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        con.close()


class FsSnapshotTests(unittest.TestCase):
    def test_snapshot_captures_db_and_lance(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            hdir = _make_hive_dir(root / "hive", rows=5)
            snap = root / "snap0"
            manifest = snapshot_fs(hdir, snap, live=True)
            self.assertIn("cognitive-memory.db", manifest["files"])
            self.assertIn("vectors.lance", manifest["files"])
            self.assertEqual(_row_count(snap / "cognitive-memory.db"), 5)
            self.assertTrue((snap / "vectors.lance" / "data.manifest").exists())
            # online backup does not carry WAL/SHM sidecars into the snapshot
            self.assertFalse((snap / "cognitive-memory.db-wal").exists())

    def test_restore_rolls_back_db_and_lance(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            hdir = _make_hive_dir(root / "hive", rows=5)
            snap = root / "snap0"
            snapshot_fs(hdir, snap, live=True)

            # mutate the live hive: add memories + a new lance fragment
            con = sqlite3.connect(hdir / "cognitive-memory.db")
            con.executemany("INSERT INTO memories (content) VALUES (?)", [("new1",), ("new2",)])
            con.commit()
            con.close()
            (hdir / "vectors.lance" / "frag2.manifest").write_text("v2")
            self.assertEqual(_row_count(hdir / "cognitive-memory.db"), 7)

            restore_fs(snap, hdir)
            self.assertEqual(_row_count(hdir / "cognitive-memory.db"), 5)  # rolled back
            self.assertFalse((hdir / "vectors.lance" / "frag2.manifest").exists())  # lance rolled back
            self.assertTrue((hdir / "vectors.lance" / "data.manifest").exists())

    def test_hive_dir_layout(self):
        p = hive_dir("/data", "proj-1", "hive-9")
        self.assertEqual(p.as_posix(), "/data/hiveminds/proj-1/hives/hive-9")


# ---- REST rollback (fake client) ----

class FakeRestClient:
    def __init__(self, mems):
        self.hives = {"h": [dict(m) for m in mems]}
        self.deleted: list = []

    def get_memories(self, hive_id, limit=100000):
        return [dict(m) for m in self.hives[hive_id]]

    def delete_memory(self, hive_id, memory_id):
        self.hives[hive_id] = [m for m in self.hives[hive_id] if m["id"] != memory_id]
        self.deleted.append(memory_id)
        return {"deleted": True}


class RestRollbackTests(unittest.TestCase):
    def _seed(self):
        return FakeRestClient([
            {"id": 1, "type": "insight", "content": "a"},
            {"id": 2, "type": "convention", "content": "b"},
        ])

    def test_snapshot_rest_records_set(self):
        client = self._seed()
        snap = snapshot_rest(client, "h", round_index=0)
        self.assertEqual(snap.count, 2)
        self.assertEqual(sorted(snap.memory_ids), [1, 2])
        self.assertEqual(snap.by_type, {"insight": 1, "convention": 1})

    def test_rollback_removes_only_post_snapshot(self):
        client = self._seed()
        snap = snapshot_rest(client, "h", round_index=0)
        # round 1 appends two learnings
        client.hives["h"] += [
            {"id": 3, "type": "insight", "content": "c"},
            {"id": 4, "type": "insight", "content": "d"},
        ]
        report = restore_rest(client, "h", snap)
        self.assertEqual(report["deleted"], 2)
        self.assertEqual(report["unrestorable_removed"], 0)
        self.assertEqual(sorted(m["id"] for m in client.get_memories("h")), [1, 2])

    def test_snapshot_json_roundtrip(self):
        client = self._seed()
        snap = snapshot_rest(client, "h", round_index=2)
        again = MemorySnapshot.from_json(snap.to_json())
        self.assertEqual(again.round_index, 2)
        self.assertEqual(again.memory_ids, snap.memory_ids)

    def test_plan_restore_diff(self):
        p = plan_restore([1, 2, 3, 4], [1, 2])
        self.assertEqual(p.to_delete, [3, 4])
        self.assertEqual(p.to_readd, [])
        p2 = plan_restore([1, 2], [1, 2, 5])
        self.assertEqual(p2.to_delete, [])
        self.assertEqual(p2.to_readd, [5])


if __name__ == "__main__":
    unittest.main()
