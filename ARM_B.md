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

## Pinning `base_commit` via a public mirror (the audit artifact)

NeoHive's git sync indexes a **branch tip**, not an arbitrary commit
(`server/src/services/sync/index.ts` → `cloneRepo(repoUrl, branch)` does
`git clone --branch <ref> --single-branch --depth 1`). It needs a remote **ref**,
not a SHA, and the hosted server can't reach a `file://` mirror on the laptop. So
per upstream project we publish a **public mirror** under `NeoHiveAI`
(`swebench-mirror-<repo>`) with a branch `swebench/<instance_id>` pinned at
`base_commit`, and point NeoHive at it.

The mirror doubles as the **open-source transparency artifact**: the bench repo
embeds it as a submodule pinned at `base_commit`, so anyone can `git clone
--recurse-submodules` and inspect the exact indexed tree, and verify
`branch SHA == dataset base_commit` (git content-addressing makes that a complete
proof of what was fed to the model — directly answering data-tainting concerns).

Publishing (`index_instance.py`): clone the upstream once (cached per repo; full
clone, since GitHub rejects shallow pushes), branch at `base_commit`, push to the
mirror. Ingestion sequence (validated against the hosted server):
1. `POST /api/hives`  header `X-HiveMind-Id: <project>`  body `{name, type:"repo", embedding_model, description}`
2. `POST /api/hives/<hiveId>/sync-configs`  `{repo_url:"https://github.com/NeoHiveAI/swebench-mirror-<repo>.git", branch:"swebench/<id>", file_blocklist}` (auto-dispatches the initial sync — do NOT re-trigger; extra triggers just re-queue redundant syncs)
3. Poll `GET …/sync-configs/<id>` until `last_indexed_sha == base_commit`.

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

## Status — full hosted loop validated end-to-end ✅ (2026-06-30, `psf__requests-1142`)

- ✅ **Mirror + audit artifact** — `NeoHiveAI/swebench-mirror-requests`, branch
  `swebench/psf__requests-1142`, remote ref **== `base_commit`** (`22623bd8c265…`).
- ✅ **Contamination guard** — passes on the clean base; flips to CONTAMINATED after
  applying the gold fix (negative test). Structural, has teeth.
- ✅ **Indexing** — `repo` hive synced the mirror branch; `last_indexed_sha == base_commit`.
- ✅ **Code-aware retrieval** — with the text model, a code-intent query returned
  `.rst` docs; after switching the hive to **`google/embeddinggemma-300m-code`** and
  re-syncing, the same query returned **source** (`requests/models.py`,
  `urllib3/filepost.py`). So per-instance hives pin `google/embeddinggemma-300m-code`.
- ✅ **Shim** — `memory_recall` scoped to the instance hive through CF, ~0.5 s.

### Embedding model
The dev box can't fit the 3584-dim `nomic-embed-code*` models ("Won't fit").
`google/embeddinggemma-300m-code` (768-dim, ~0.5 GB, code-mode) fits and is what
makes Arm B a *code* retrieval test. To change a hive's model you do NOT delete +
recreate — change it in place in the FE (triggers re-embed) or `POST …/re-embed`.

## Remaining

- **Submodule wiring.** Embed each mirror as a submodule (`indexed/<instance_id>`,
  pinned at `base_commit`) in this bench repo — the public audit trail.
- **Run the A/B.** Run `run_arm_b.sh` for an instance and compare resolved-rate vs
  the Arm-A control on the same instance.
- **Scale + publish-method.** `index_instance.py` currently full-clones + pushes per
  upstream (cached). For the full 500, prefer server-side fork + `create-ref` (no
  local transfer). First sync per fresh model is slow (~6 min: download + warmup).
- **In-container python.** The shim is invoked as `python /opt/neohive/neohive_search.py`;
  confirm the testbed conda `python` is on PATH under `bash -c` (BASH_ENV sources
  the image's `.bashrc` → `conda activate testbed`) on the first real Arm-B run.

## Run

```sh
# 5-instance Arm-B smoke (CF creds come from ~/.zshrc; needs a model key).
# NeoHive has no Auth0, so no PAT — CF token only. Project "SWE Bench":
OPENROUTER_API_KEY=... NEOHIVE_BASE=https://neohive.logilica.com \
NEOHIVE_PROJECT=e873808e-5333-4641-9f25-c75bbc5744ff \
./run_arm_b.sh openrouter/z-ai/glm-5.2 0:5

# then grade exactly like Arm A:
./grade_swebench.sh results/armb-<...>/preds.json armb-glm52-smoke 2
```
