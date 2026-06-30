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

## Target: hosted `neohive.logilica.com` (Cloudflare Access, no Auth0/PAT)

Decided 2026-06-30: index into + search the hosted instance. Auth facts learned
on first contact:
- **No Auth0 → no NeoHive PAT.** Access is gated by **Cloudflare Access**; send the
  CF service-token headers `CF-Access-Client-Id` / `CF-Access-Client-Secret`
  (from `~/.zshrc`: `NEOHIVE_CF_ACCESS_CLIENT_ID` / `NEOHIVE_CF_ACCESS_CLIENT_SECRET`).
  Past CF, NeoHive attaches owner context itself.
- **MCP route is `/projects/:id/mcp`** on this deployment (NOT `/hiveminds/:id/mcp`).
- **REST is `/api/hives` + `X-HiveMind-Id: <project>` header** (gateway-level).
- **CF WAF Error 1010 gotcha:** Cloudflare blocks the default `Python-urllib/x.y`
  User-Agent. The shim + indexer set an explicit `User-Agent` (any non-default UA
  passes). Without it every request 403s — and the in-container shim hits this too.
- The container reaches the **public** host directly (normal egress in the agent
  phase) — no `host.docker.internal`, no `--add-host`.

## Status

- ✅ **Shim (HIVE-265) validated end-to-end** against the hosted NeoHive through CF
  (`initialize` → `memory_recall` returns results, ~0.5 s).
- ✅ **Contamination guard (HIVE-268) validated offline** (psf__requests-1142, + a
  negative test).
- ⛔ **Indexing ingestion (HIVE-268) BLOCKED on hosted.** The `file://` local-mirror
  trick assumed a co-located NeoHive; the hosted server cannot reach a `file://`
  path on this laptop. And NeoHive's sync `cloneRepo` does
  `git clone --branch <branch> --single-branch --depth 1` — it needs a remote
  **ref** (branch/tag), not a SHA, so it can neither pin `base_commit` from the
  public repo nor accept a local mirror. (Code-via-`/uploads` is capped at 20
  files/batch + uses the markdown path, so it is not a viable whole-repo route.)

  Resolving this is a fork (pending decision):
  - **A — add commit-pinning to NeoHive sync** (clone full + `git checkout <commit>`,
    new `commit`/`ref` field on sync-config). Clean, general, dogfoods NeoHive;
    needs a MemVec PR + redeploy of the hosted instance.
  - **B — push synthetic `swebench-<id>` branches to a throwaway remote** NeoHive can
    clone (e.g. a private logilica GitHub repo). No server change; per-instance
    push overhead + cleanup.

## Other open items

- **In-container python.** The shim is invoked as `python /opt/neohive/neohive_search.py`;
  confirm the testbed conda `python` is on PATH under `bash -c` (BASH_ENV sources
  the image's `.bashrc` → `conda activate testbed`).
- **Embedding model.** `repo` hives default to `nomic-embed-text-v2-moe`; decide
  whether to pin a code-specific model via `NEOHIVE_EMBEDDING_MODEL` for fairness.

## Run

```sh
# 5-instance Arm-B smoke (CF creds come from ~/.zshrc; needs a model key + the
# indexing fork resolved). NeoHive has no Auth0, so no PAT — CF token only:
OPENROUTER_API_KEY=... NEOHIVE_BASE=https://neohive.logilica.com \
NEOHIVE_PROJECT=<uuid> \
./run_arm_b.sh openrouter/z-ai/glm-5.2 0:5

# then grade exactly like Arm A:
./grade_swebench.sh results/armb-<...>/preds.json armb-glm52-smoke 2
```
