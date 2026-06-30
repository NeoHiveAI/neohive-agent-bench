# Arm B — the NeoHive treatment arm (HIVE-265 + HIVE-268)

Arm B is Arm A's *exact* harness plus NeoHive: each SWE-bench instance's repo is
indexed at `base_commit` into its own NeoHive hive, and the agent is given a
`neohive-search` command to retrieve over it. Comparing Arm A vs Arm B resolved
rates per model is the whole experiment.

## The key reframe: bash shim, not MCP injection

HIVE-265 was originally written assuming a Claude-Code-style agent ("inject the
MCP connection + getting-started skill"). Our chosen scaffold is **mini-swe-agent,
which is bash-only** — it has no MCP client and no skill system. Its single
actuator is bash commands, executed **inside** the per-instance container
(`docker exec`, cwd `/testbed`).

Consequently NeoHive cannot be a host-side MCP tool (HIVE-263's "Pattern 1 is
free"). Retrieval has to be a **bash command the agent runs in the container**
that reaches the host NeoHive — the in-container "Pattern 2" case the HIVE-263
probe was built to de-risk. So HIVE-265 becomes: install a `neohive-search` shim
+ teach the agent about it via a one-key system-prompt overlay.

## Components

| File | Role |
|---|---|
| `index_instance.py` | HIVE-268: clone repo @ `base_commit` → synthetic branch → contamination guard → create + index a per-instance NeoHive hive. |
| `neohive_search.py` | HIVE-265: the in-container shim. `memory_recall` over the instance's hive via MCP (stdlib only); prints ranked code chunks. |
| `config/arm_b_neohive.yaml` | HIVE-265: mini-swe-agent overlay. Adds the shim mount, host networking, forwarded env, and the system-prompt affordance — and nothing else. |
| `run_arm_b.sh` | Orchestrator: per-instance loop (index → set env → run one instance), merging into one `preds.json`. |

## Contamination guard — structural, and validated

Both the gold `patch` (the fix) and the `test_patch` (the tests that check it) are
diffs applied *on top of* `base_commit` — the fix at agent time, the tests at
grade time. So the pristine `base_commit` tree contains **neither**. We index
exactly at `base_commit` and `index_instance.py` asserts it: each patch must
**apply forward** (we are pre-fix) and **not apply in reverse** (the answer is
absent). If the fix were already present, reverse would apply → the run aborts.

Validated offline on `psf__requests-1142` (2026-06-30): clean base passes both
checks; after applying the gold fix, the reverse check flips to "already present"
→ the guard correctly reports CONTAMINATED. The guard has teeth.

A `file_blocklist` of test dirs is also sent to the sync-config as defense in
depth, but the structural guard is the real guarantee.

## Pinning `base_commit` despite branch-only sync

NeoHive's git sync indexes a **branch tip**, not an arbitrary commit
(`server/src/services/sync/index.ts` → `cloneRepo(repoUrl, branch)`). To pin a
commit we clone locally, create branch `swebench-base` at `base_commit`, and
point a sync-config at that local repo (`repo_url=file://…`, `branch=swebench-base`).
This reuses NeoHive's real code-embedding path (a `repo`-type hive, server-default
code embeddings) — which is what makes Arm B a fair test — with no GitHub remote
or credentials.

Ingestion sequence (confirmed against server routes):
1. `POST /api/hives`  header `X-HiveMind-Id: <project>`  body `{name, type:"repo", description}`
2. `POST /api/hives/<hiveId>/sync-configs`  `{repo_url:"file://…", branch:"swebench-base", file_blocklist}` (auto-dispatches the initial sync)
3. Poll `GET …/sync-configs/<id>` until `last_indexed_sha` is set.

## Per-instance isolation → one-at-a-time loop

Each instance needs retrieval scoped to *its* repo. The shim takes `NEOHIVE_HIVE`
to scope `memory_recall` to one hive. mini-swe-agent's batch runner uses one
config for the whole batch, so per-instance hive scoping forces a one-instance-per-
invocation loop (`--filter "^<id>$"`). All runs merge into one `preds.json`
(`update_preds_file` keys by instance_id), so grading is unchanged.

## Open items — verify on first live run (needs a reachable NeoHive)

These are the knobs that can only be nailed against a live instance (exactly what
HIVE-263 flagged):

1. **Target NeoHive + creds (BLOCKER).** No local NeoHive is running, and the dev
   box moved to a hosted `https://neohive.logilica.com`. Arm B needs `NEOHIVE_BASE`
   + `NEOHIVE_PROJECT` + a `NEOHIVE_PAT`. Recommended: stand up a dedicated local
   NeoHive for the bench (isolated, reproducible, container reaches it via
   host.docker.internal, no rate limits).
2. **`file://` clone.** Confirm NeoHive's `cloneRepo` accepts a `file://` URL +
   arbitrary branch. If it rejects non-GitHub URLs, fall back to the `clone_path`
   field or push `swebench-base` to a throwaway remote.
3. **Container → host networking on Colima.** Confirm `--add-host=host.docker.internal:host-gateway`
   actually resolves to the macOS host (where NeoHive runs) from inside the
   emulated container. Run `mcp_reachability_probe.sh` first to flip HIVE-263 green.
4. **Auth header.** `Authorization: Bearer pat_…` assumed; if the gateway 401s,
   switch the shim + indexer to `x-api-key` (one-line toggle).
5. **In-container python.** The shim is invoked as `python /opt/neohive/neohive_search.py`;
   confirm the testbed conda `python` is on PATH under `bash -c` (BASH_ENV sources
   the image's `.bashrc` → `conda activate testbed`).
6. **Embedding model.** `repo` hives default to the server's repo model; decide
   whether to pin a code-specific model via `NEOHIVE_EMBEDDING_MODEL` for fairness.

## Run

```sh
# 5-instance Arm-B smoke (needs a reachable NeoHive + PAT + a model key):
OPENROUTER_API_KEY=... NEOHIVE_BASE=http://localhost:3577 \
NEOHIVE_PROJECT=<uuid> NEOHIVE_PAT=pat_... \
./run_arm_b.sh openrouter/z-ai/glm-5.2 0:5

# then grade exactly like Arm A:
./grade_swebench.sh results/armb-<...>/preds.json armb-glm52-smoke 2
```
