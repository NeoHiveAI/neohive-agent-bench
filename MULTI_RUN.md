# Multi-Run / compounding-memory loop (HIVE-334 … HIVE-343)

Turns the single-pass A/B harness into a **compounding loop**: one persistent per-repo
experience pool that the agent reads *and* writes across rounds, so resolve rate on
**fresh, unseen** issues rises as memory accumulates. The deliverable is a
**treated-vs-memoryless-twin trend**, not a single score — the signal is the *slope*
across rounds and the *gap* to the twin.

The single-pass A/B path is unchanged: everything here is behind flags
(`--compounding` / `--twin`), off by default.

## Target: a LOCAL NeoHive on the run host

The compounding run points at a **local** NeoHive (default `http://127.0.0.1:3577`, dev
auth, its data dir on local disk) — NOT the hosted CF deployment. That is deliberate and
load-bearing: it makes HIVE-334 snapshots **byte-exact** (a filesystem copy of the hive
dir), gives dev auth (no Cloudflare), and lets us create a fresh project per repo-career.

Env used by the loop:

| Var | Meaning | Default |
|---|---|---|
| `NEOHIVE_BASE` | MCP/REST base the **in-container** agent + smart-prompts use | `https://neohive.logilica.com` (set to `http://host.docker.internal:3577` for local) |
| `NEOHIVE_HOST_BASE` | base the **host-side** reflect-and-store uses | `http://127.0.0.1:3577` |
| `NEOHIVE_PROJECT` | the career project id | — |
| `NEOHIVE_DATA_DIR` | local NeoHive data dir (for FS snapshots) | — |
| `OPENROUTER_API_KEY` | agent + GLM-4.6 reflect model | — |

## Design adaptations (forced by the server's real surface — MemVec is unchanged)

Verified against the running server + indexed server code:

1. **No native snapshot/restore.** `memory_snapshots` / `/api/hives/:id/snapshots` are
   daily *count telemetry*, not backups. → HIVE-334 is **app-level**: FS byte-exact
   snapshot (`cognitive-memory.db` via SQLite online-backup + `vectors.lance/`), and a
   REST soft-delete rollback for live append-only dose control.
2. **`memory_store` (MCP) auto-routes to the project's *Knowledge* hive** and takes no
   `hive` param — it can't write into a `repo` hive. → the written experience pool is
   the **Knowledge hive of a dedicated per-repo-career project**; the code `repo` hive
   stays read-only/git-synced. Cross-hive recall *within that project* surfaces both.
3. **The filter can't run server-side.** → reflect-and-store runs **host-side** (reflect
   → filter → `memory_store`), and the agent's own unfiltered `memory_store` is disabled
   in compounding mode (`NEOHIVE_AGENT_WRITE_DISABLED`, guarded in `plugin/neohive.ts`),
   so the HIVE-336 filter is authoritative.

## Components

| Ticket | File(s) | What |
|---|---|---|
| 334 | `hive_snapshot.py`, `neohive_rest.py` | persistent lifecycle + per-round snapshot (FS byte-exact) / restore (FS) / rollback (REST) |
| 335 | `reflect_and_store.py` | host-side, filter-gated reflect-and-store (default reflector = GLM-4.6) |
| 336 | `solution_copy_filter.py` | block only long verbatim spans of gold `patch`/`test_patch`; keep real learnings |
| 337/338 | `run_rounds.py` | rolling-rounds runner + memoryless twin (identical slices/order) |
| 341 | `models.json`, `models.py` | pinned agent set + GLM-4.6 fixed helper + seeds |
| 342 | `cl_metrics.py` | GEM (ACC/BWT/FWT/forgetting) + improvement-curve slope + treated-vs-twin gap |
| 343 | `grading.py` | post-process the STOCK swebench report; apply-failures in their own bucket |
| integration | `run_opencode.py` | `--compounding` / `--twin` flags; single-pass untouched when off |

Tests: `python3 -m unittest discover -s tests -p 'test_*.py'` (62 tests, stdlib-only, no venv).

## The STOP-AND-VALIDATE spike (2–3 django instances)

| Check | Status here | How it's verified |
|---|---|---|
| (a) hive persists & grows across the career | **machinery PASS (live)** | `spike_neohive_machinery.py` — grows 0→2→4, FS snapshot, exact rollback; agent-driven growth is the x86 run |
| (b) filter blocks planted verbatim patch, keeps planted general note | **PASS (offline)** | `tests/test_solution_copy_filter.py` + `tests/test_reflect_and_store.py` |
| (c) twin runs same instances, memory off | **sequencing PASS (offline)** | `run_rounds.py --dry-run` (treated climbs, twin flat, identical slices); full run needs Docker |
| (d) grading byte-identical to stock swebench | **path preserved** | `grade_swebench.sh` calls stock `swebench.harness.run_evaluation` untouched; `grading.py` only post-processes its output |

This machine has **no Docker / no `OPENROUTER_API_KEY` / no `.venv`**, so (c)-full and
(d)-full — which need the agent-in-container + swebench images — run on the **x86 host**.

### Running the full spike on the x86 host

```sh
# 0) local NeoHive up on the host; venv with swebench+datasets; Docker running
export NEOHIVE_HOST_BASE=http://127.0.0.1:3577
export NEOHIVE_BASE=http://host.docker.internal:3577   # in-container reaches the host
export NEOHIVE_DATA_DIR=/path/to/neohive/.localdata
export OPENROUTER_API_KEY=...

# (a) NeoHive machinery (no Docker needed)
python3 spike_neohive_machinery.py

# 1) build the django timeline (HIVE-340) -> chronologically ordered instance ids
#    (a JSON list). Create one treated project + one twin project (separate Knowledge
#    hives); index the django repo hive into each (index_instance.py).

# (c)+(d) treated then twin careers on the SAME timeline (identical slices/order)
NEOHIVE_PROJECT=<treated_proj> python3 run_rounds.py --repo django/django \
  --timeline django_timeline.json --mode treated --model z-ai/glm-5.2 \
  --round-size 2 --hive-id <treated_knowledge_hive> --data-dir "$NEOHIVE_DATA_DIR"
NEOHIVE_PROJECT=<twin_proj> python3 run_rounds.py --repo django/django \
  --timeline django_timeline.json --mode twin --model z-ai/glm-5.2 \
  --round-size 2 --hive-id <twin_knowledge_hive> --data-dir "$NEOHIVE_DATA_DIR"

# grading is stock (run_rounds calls grade_swebench.sh -> swebench.harness.run_evaluation);
# grading.py buckets apply-failures apart. cl_metrics.improvement_curve(treated, twin)
# gives the slope + gap; gem_metrics(R, baseline) gives ACC/BWT/FWT/forgetting.
```

## Guardrails honored

- **No semantic/theme dedup** — the filter blocks verbatim solution/test spans only.
- **Grading harness unmodified** — stock `swebench`, identical for every arm; apply
  failures are a separate bucket, never counted as unresolved.
- **No per-instance hive wipe in compounding mode** — the pool persists across the career.
- **GLM-4.6 fixed** as smart-prompts rewriter *and* reflect model across all arms.
- **qemu only for the tiny spike**; pilot + full run on the native x86 host.
