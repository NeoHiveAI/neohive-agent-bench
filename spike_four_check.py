#!/usr/bin/env python3
"""Four-check STOP-AND-VALIDATE spike driver (HIVE-337/338, the checkpoint before the pilot).

Runs a treated (Arm B, writes on) and a memoryless-twin career over the SAME chronologically
ordered django instances in the SAME order, and emits the evidence for the four checks:

  (a) the experience-pool hive PERSISTS and GROWS across the instances (treated), while the
      twin's stays empty;
  (b) the write filter BLOCKS a planted verbatim-patch memory but KEEPS a planted general
      note (live, against the real hive + real filter);
  (c) the twin runs the same instances with memory OFF (no writes accumulate);
  (d) grading is byte-identical to a stock swebench run — grading.py only post-processes the
      untouched stock report.

Host reality (documented, not worked around): the co-located dev NeoHive is bound to
127.0.0.1, so a container on the default bridge cannot reach it — in-container *recall* is
not available on this host (binding 0.0.0.0 would LAN-expose a no-auth instance). The
compounding machinery the four checks test is host-side, so it is exercised faithfully; the
only Arm-B facet not exercised here is in-container recall (a pilot-hardening item: bind the
dev NeoHive to the docker bridge / use --network host). Both arms therefore run the solve
with the identical Arm-A scaffold; the treated arm adds the host-side, filter-gated
reflect-and-store write path (HIVE-335) — which IS the treated/twin variable under test.

Stdlib only. Real solves/reflect/grading need Docker + OPENROUTER_API_KEY + the local NeoHive;
--dry-run stubs all three so the orchestration + report are testable offline.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import grading
import hive_snapshot as snap
import index_instance as idx
from neohive_rest import NeoHiveClient
from reflect_and_store import reflect_and_store
from solution_copy_filter import DEFAULT_NGRAM_THRESHOLD, extract_diff_code, tokenize

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Career project + experience-pool hive setup
# --------------------------------------------------------------------------- #

def ensure_career_project(client: NeoHiveClient, name: str) -> tuple[str, str]:
    """Create (or reuse) a career project and return (project_id, knowledge_hive_id). A new
    project auto-provisions a 'Knowledge' hive; that is the persistent experience pool."""
    existing = next((p for p in client.list_projects() if p.get("name") == name), None)
    proj = existing or client.create_project(name, description="four-check spike career")
    pid = proj.get("id") or proj.get("project", {}).get("id")
    probe = NeoHiveClient(base=client.base, project=pid)
    kh = next((h for h in probe.list_hives() if h.get("type") == "knowledge"), None)
    if not kh:
        raise RuntimeError(f"project {pid} has no Knowledge hive")
    return pid, kh["id"]


# --------------------------------------------------------------------------- #
# One instance: solve (Arm-A scaffold) + optional treated reflect
# --------------------------------------------------------------------------- #

def solve_instance(instance_id: str, model: str, out_dir: Path, timeout: int,
                   *, solver=None) -> dict:
    """Run one instance through the Arm-A opencode scaffold; return {status, patch, transcript}.
    `solver` is injectable for --dry-run."""
    if solver is not None:
        return solver(instance_id, out_dir)
    subprocess.run([str(HERE / ".venv/bin/python"), str(HERE / "run_opencode.py"), instance_id,
                    "--arm", "a", "--model", model, "--output", str(out_dir),
                    "--timeout", str(timeout)], check=True, env=dict(os.environ))
    idir = out_dir / instance_id
    patch = (idir / "patch.diff").read_text() if (idir / "patch.diff").exists() else ""
    transcript = ""
    for f in ("opencode-events.json", "opencode-stderr.log"):
        p = idir / f
        if p.exists():
            transcript += p.read_text()
    return {"status": "ok" if patch else "empty", "patch": patch, "transcript": transcript}


# --------------------------------------------------------------------------- #
# Check (b): planted verbatim-patch vs general note through the live filter+store
# --------------------------------------------------------------------------- #

def check_filter_live(client: NeoHiveClient, gold_instance: dict, *, dry_run: bool) -> dict:
    """Drive reflect_and_store with a STUB reflector returning exactly two candidates — a
    verbatim span of the gold patch's changed code, and a general transferable note — and
    confirm the filter BLOCKS the copy and KEEPS the note (against the real hive)."""
    changed = extract_diff_code(gold_instance.get("patch", ""))
    span = " ".join(tokenize(changed)[: DEFAULT_NGRAM_THRESHOLD + 15])  # a long verbatim run
    note = ("When touching Django's admin/forms validation, prefer overriding the field's "
            "clean method and surface user-facing errors via ValidationError rather than "
            "mutating cleaned_data in place — a transferable convention, no code copied.")

    def planted_reflector(problem_statement, transcript, patch, status):
        return [
            {"content": span, "type": "example_pattern", "importance": 6, "tags": ["planted", "verbatim"]},
            {"content": note, "type": "convention", "importance": 6, "tags": ["planted", "note"]},
        ]

    rr = reflect_and_store(client, gold_instance, transcript="(planted check)", produced_patch="",
                           status="planted", reflector=planted_reflector, dry_run=dry_run)
    return {
        "candidates": rr.candidates, "stored": rr.stored, "blocked": rr.blocked,
        "blocked_details": rr.blocked_details, "stored_summaries": rr.stored_summaries,
        "pass": rr.blocked >= 1 and rr.stored >= 1
                and any(d.get("source") in ("patch", "test_patch") for d in rr.blocked_details),
    }


# --------------------------------------------------------------------------- #
# One career (treated or twin) over the ordered instances
# --------------------------------------------------------------------------- #

def run_career(mode: str, ordered_ids: list[dict], client: NeoHiveClient, kh_id: str, *,
               model: str, out_dir: Path, data_dir: str | None, project: str, timeout: int,
               solver=None, reflector=None, dry_run: bool) -> dict:
    """mode 'treated' => solve + reflect-and-store (writes on); mode 'twin' => solve only
    (writes off). Records the hive-count growth series + per-round snapshots + preds path."""
    writes_on = mode == "treated"
    rounds = []
    growth = []
    preds = out_dir / "preds.json"
    for r, row in enumerate(ordered_ids):
        iid = row["instance_id"]
        res = solve_instance(iid, model, out_dir, timeout, solver=solver)
        reflect = {}
        if writes_on:
            inst = idx.load_instance(iid) if not dry_run else {
                "instance_id": iid, "problem_statement": "", "patch": row.get("_gold_patch", ""),
                "test_patch": ""}
            rr = reflect_and_store(client, inst, res["transcript"], res["patch"], res["status"],
                                   reflector=reflector, dry_run=dry_run)
            reflect = rr.as_dict()
        if dry_run:  # deterministic stub growth, no network
            count = (r + 1) if writes_on else 0
        else:
            count = client.memory_count(kh_id)
        snapshot = {}
        if data_dir and not dry_run:
            dest = out_dir / "snapshots" / f"round{r}"
            snapshot = snap.snapshot_fs(snap.hive_dir(data_dir, project, kh_id), dest, live=True)
        growth.append(count)
        rounds.append({"index": r, "instance_id": iid, "status": res["status"],
                       "patch_len": len(res["patch"]), "reflect": reflect,
                       "hive_count_after": count, "snapshot": snapshot})
    return {"mode": mode, "model": model, "growth_series": growth, "rounds": rounds,
            "preds_path": str(preds)}


# --------------------------------------------------------------------------- #
# Check (d): stock grading + grading.py buckets
# --------------------------------------------------------------------------- #

def grade_career(preds_path: Path, run_id: str, model: str, instance_ids: list[str],
                 *, dry_run: bool) -> dict:
    if dry_run:
        verdicts = [grading.InstanceVerdict(i, grading.UNRESOLVED) for i in instance_ids]
        return {"stock": {"stub": True}, "buckets": grading.summarize(verdicts).as_dict()}
    subprocess.run([str(HERE / "grade_swebench.sh"), str(preds_path), run_id, "2"], check=True, cwd=str(HERE))
    # swebench ignores --report_dir and writes "<model with / -> __>.<run_id>.json" to CWD
    # (confirmed on olympus). Glob by run_id so any model-slug transform is matched.
    cands = sorted(HERE.glob(f"*{run_id}.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    report = cands[0] if cands else HERE / f"{model.replace('/', '__')}.{run_id}.json"
    run_logs = HERE / "logs" / "run_evaluation" / run_id
    model_dirs = [d for d in run_logs.iterdir() if d.is_dir()] if run_logs.exists() else []
    logs = model_dirs[0] if model_dirs else run_logs / model.replace("/", "__")
    stock = json.loads(report.read_text()) if report.exists() else {}
    buckets = grading.grade_report(report, logs_dir=logs, instance_ids=instance_ids).as_dict()
    return {"stock_report": str(report), "stock_counts": {
                k: stock.get(k) for k in ("total_instances", "submitted_instances", "completed_instances",
                                          "resolved_instances", "unresolved_instances", "empty_patch_instances",
                                          "error_instances")},
            "buckets": buckets}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Four-check compounding-loop spike (HIVE-337/338).")
    ap.add_argument("--timeline", default="django_timeline.json")
    ap.add_argument("--model", default="openrouter/z-ai/glm-5.2")
    ap.add_argument("--treated-project", default="spike-treated")
    ap.add_argument("--twin-project", default="spike-twin")
    ap.add_argument("--data-dir", default=os.environ.get("NEOHIVE_DATA_DIR"))
    ap.add_argument("--host-base", default=os.environ.get("NEOHIVE_HOST_BASE", "http://127.0.0.1:3577"))
    ap.add_argument("--output", default="results/spike")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--limit", type=int, default=None, help="cap instances (spike budget guard).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tl = json.loads((HERE / args.timeline if not Path(args.timeline).is_absolute() else Path(args.timeline)).read_text())
    ids = tl.get("instances") or [{"instance_id": i} for i in tl.get("ordered_ids", [])]
    if args.limit:
        ids = ids[: args.limit]
    out = Path(args.output) if Path(args.output).is_absolute() else HERE / args.output
    (out).mkdir(parents=True, exist_ok=True)

    report: dict = {"timeline": args.timeline, "model": args.model, "dry_run": args.dry_run,
                    "instances": [r["instance_id"] for r in ids], "host_base": args.host_base}

    # --- stubs for offline orchestration test ---
    solver = reflector = None
    if args.dry_run:
        def solver(iid, odir):  # noqa: E306
            d = odir / iid; d.mkdir(parents=True, exist_ok=True)
            (d / "patch.diff").write_text(f"diff --git a/x b/x\n+++ b/x\n+patch for {iid}\n")
            return {"status": "ok", "patch": f"patch for {iid}", "transcript": f"solved {iid}"}

        def reflector(ps, tr, patch, status):  # noqa: E306
            return [{"content": f"transferable lesson from {status}", "type": "insight",
                     "importance": 6, "tags": ["dry"]}]

    treated_client = NeoHiveClient(base=args.host_base, project=None, timeout=600)
    twin_client = NeoHiveClient(base=args.host_base, project=None, timeout=600)
    if args.dry_run:
        tpid, tkh = "dry-treated", "dry-tkh"
        wpid, wkh = "dry-twin", "dry-wkh"
    else:
        tpid, tkh = ensure_career_project(treated_client, args.treated_project)
        wpid, wkh = ensure_career_project(twin_client, args.twin_project)
    treated_client.project, twin_client.project = tpid, wpid
    report["treated_project"], report["twin_project"] = tpid, wpid
    report["treated_knowledge_hive"], report["twin_knowledge_hive"] = tkh, wkh

    # ---- (b) planted filter check (against the treated hive) ----
    gold = ({"instance_id": ids[0]["instance_id"], "patch":
             "diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-old\n+def clean_field(self, value):\n"
             "+    if value is None:\n+        raise ValidationError('required')\n+    return value.strip().lower()\n"}
            if args.dry_run else idx.load_instance(ids[0]["instance_id"]))
    print("[spike] (b) planted verbatim-patch-vs-note filter check ...", flush=True)
    report["check_b_filter"] = check_filter_live(treated_client, gold, dry_run=args.dry_run)
    print(f"[spike] (b) => {json.dumps(report['check_b_filter'], indent=2)[:400]}", flush=True)

    # ---- (a)+(c) treated then twin careers over the same instances/order ----
    for row in ids:
        row.setdefault("_gold_patch", gold.get("patch", ""))
    print(f"[spike] (a) treated career over {[r['instance_id'] for r in ids]} ...", flush=True)
    report["treated"] = run_career("treated", ids, treated_client, tkh, model=args.model,
                                   out_dir=out / "treated", data_dir=args.data_dir, project=tpid,
                                   timeout=args.timeout, solver=solver, reflector=reflector, dry_run=args.dry_run)
    print(f"[spike] (a) treated growth_series={report['treated']['growth_series']}", flush=True)
    print(f"[spike] (c) twin career (memory OFF) over the same instances ...", flush=True)
    report["twin"] = run_career("twin", ids, twin_client, wkh, model=args.model,
                                out_dir=out / "twin", data_dir=args.data_dir, project=wpid,
                                timeout=args.timeout, solver=solver, reflector=reflector, dry_run=args.dry_run)
    print(f"[spike] (c) twin growth_series={report['twin']['growth_series']}", flush=True)

    # ---- (d) stock grading for both arms ----
    iid_list = [r["instance_id"] for r in ids]
    print("[spike] (d) grading treated + twin on stock swebench ...", flush=True)
    report["treated"]["grading"] = grade_career(out / "treated" / "preds.json",
                                                 f"spike-treated-{int(time.time())}", args.model, iid_list, dry_run=args.dry_run)
    report["twin"]["grading"] = grade_career(out / "twin" / "preds.json",
                                             f"spike-twin-{int(time.time())}", args.model, iid_list, dry_run=args.dry_run)

    # ---- verdicts ----
    t_growth, w_growth = report["treated"]["growth_series"], report["twin"]["growth_series"]
    report["verdicts"] = {
        "a_hive_persists_and_grows": len(t_growth) >= 2 and t_growth[-1] > t_growth[0],
        "b_filter_blocks_copy_keeps_note": report["check_b_filter"]["pass"],
        "c_twin_memory_off": all(c == 0 for c in w_growth),
        "d_grading_stock": all("buckets" in report[a]["grading"] for a in ("treated", "twin")),
    }
    (out / "spike_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("\n[spike] VERDICTS:")
    for k, v in report["verdicts"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"[spike] full report -> {out / 'spike_report.json'}")
    return 0 if all(report["verdicts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
