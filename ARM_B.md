# Arm B — the NeoHive treatment arm (HIVE-265 + HIVE-268)

Arm B is Arm A's *exact* harness plus NeoHive **connected as a real MCP server**:
each SWE-bench instance's repo is indexed at `base_commit` into a NeoHive hive, and
the agent uses NeoHive's MCP tools (`memory_recall`, `memory_context`, …) natively
and autonomously — exactly how a NeoHive end-user's coding agent does. Comparing
Arm A vs Arm B resolved rates per model is the whole experiment.

## Approach: opencode + NeoHive MCP (not a bash shim)

The scaffold is **opencode** (model-agnostic via OpenRouter, MCP-native), run
**inside** each SWE-bench container so the agent has the repo's deps and can run
tests. Both arms are the same opencode + model; the difference is NeoHive:

- **Arm A** — `config/opencode-arm-a.json` (`permission: {"*":"allow"}`, nothing else).
- **Arm B** — the **full faithful NeoHive end-user setup** — everything
  `/neohive:getting-started` provisions, with every feature enabled. The official
  NeoHive opencode port (the team's adaptation of `NeoHiveAI/NeoHiveClaude`) is
  committed under `config/neohive-opencode/` and assembled into the container's
  `/root/.config/opencode/`:
  - `mcp.neohive` remote server (instance's project, CF service token in `headers`);
  - `instructions/neohive.md` — usage rules auto-loaded every session;
  - `plugin/neohive.ts` — glob/grep→`memory_recall` nudge;
  - `plugin/neohive-smart-prompts.ts` — the `enable-smart-prompts` auto-context layer
    (rewrite prompt → `memory_recall` → filter → inject), adapted for CF + OpenRouter;
  - `agents/explore-neohive.md` — recall-first exploration subagent;
  - `skill/*` — the NeoHive skills.

  This is "NeoHive exactly as a power-user end-user would have it," committed in full
  so the setup is transparent and critiquable (see `config/neohive-opencode/PROVENANCE.md`).

This replaces the earlier hand-rolled `neohive-search` bash shim, which only
measured "agent runs a command we told it to" rather than "agent uses NeoHive like
a user would". Because opencode is a real MCP client running the agent loop, the
NeoHive MCP connection is **host-/agent-side relative to the model** and reaches
NeoHive's public CF endpoint directly — vindicating HIVE-263's "Pattern 1".

Validated end-to-end (2026-06-30/07-01): opencode 1.17.10 mounted into the
`psf__requests-1142` image (runs under amd64 emulation) → `opencode mcp list`
shows `neohive connected` through CF → `opencode run` with `glm-4.6` calls
`neohive_memory_recall` and returns source (`requests/models.py`,
`urllib3/filepost.py`). CF gotcha: opencode's MCP client must send an explicit
`User-Agent` header (CF WAF Error 1010 blocks default UAs).

## Components

| File | Role |
|---|---|
| `index_instance.py` | HIVE-268: clone repo @ `base_commit` → branch → contamination guard → publish public mirror → create + index a per-instance code-embedding NeoHive hive. |
| `fetch_opencode.sh` | Fetch the pinned opencode linux-x64 binary (mounted into every container). |
| `config/opencode-arm-a.json` / `-arm-b.json` | The two opencode `opencode.json` configs (Arm A bare; Arm B adds mcp + instructions + plugin refs). |
| `config/neohive-opencode/` | The full faithful NeoHive opencode setup committed for transparency (rules, both plugins incl. smart-prompts, explore-neohive subagent, skills) + `PROVENANCE.md`. Arm B only. |
| `run_opencode.py` | Per-instance, per-arm runner: `docker run` the image with opencode mounted → assemble + `docker cp` the arm's `/root/.config/opencode/` → `opencode run "<problem_statement>"` in `/testbed` → `git diff` → `preds.json`. Arm B purges + indexes the hive (temporal isolation) and deletes it after. Grade with `grade_swebench.sh`. |

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

## Per-instance isolation → one hive in the project at a time

Each instance's agent must retrieve only over *its* repo. The agent calls
`memory_recall` without a hive arg, so recall fans across **all hives in the
project** — meaning a shared project would leak other instances' repos. Per-instance
*projects* aren't feasible (hosted project-create is broken, dashboard-only). So we
enforce isolation **temporally**: the runner keeps exactly one hive in the "SWE
Bench" project at a time — index instance → run opencode → delete the hive → next.
The per-instance runs each write their own `preds.json`; grading is unchanged.

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
  User-Agent. opencode's MCP client (via the config `headers`) and the indexer both
  set an explicit `User-Agent` (any non-default UA passes). Without it every request
  403s — and opencode in-container hits this too.
- opencode in-container reaches the **public** NeoHive host directly (normal egress
  in the agent phase) — no `host.docker.internal`, no `--add-host`.

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
- ✅ **opencode MCP (host test)** — `memory_recall` scoped to the instance hive through CF, ~0.5 s.
- ✅ **opencode in-container** — pinned 1.17.10 binary mounted into the `requests-1142`
  image (amd64 emulation); `opencode mcp list` → `neohive connected` via CF; `opencode
  run` with `glm-4.6` called `neohive_memory_recall` and returned source. The full
  in-container Arm-B mechanism works.

### Embedding model
The dev box can't fit the 3584-dim `nomic-embed-code*` models ("Won't fit").
`google/embeddinggemma-300m-code` (768-dim, ~0.5 GB, code-mode) fits and is what
makes Arm B a *code* retrieval test. To change a hive's model you do NOT delete +
recreate — change it in place in the FE (triggers re-embed) or `POST …/re-embed`.

## Remaining

- **First A/B** — `run_opencode.py` is built (helpers + full-faithful config assembly
  validated; the in-container opencode+MCP flow is validated). Run both arms on
  `psf__requests-1142` and compare resolved-rate, then expand to the pilot. First real
  run still needs to confirm: the smart-prompts nested `opencode run` calls work under
  emulation, and the agent edits files headlessly (permission `{"*":"allow"}`).
- **Methodology — RESOLVED:** Arm B = the full faithful end-user setup with **every
  feature enabled** (rules + grep-nudge + smart-prompts auto-context + explore-neohive
  + skills), committed under `config/neohive-opencode/`. Not a stripped single-variable
  setup — the comparison is "NeoHive as a power-user has it" vs baseline.
- **Submodule wiring.** Embed each mirror as a submodule (`indexed/<instance_id>`,
  pinned at `base_commit`) in this bench repo — the public audit trail.
- **Scale + publish-method.** `index_instance.py` full-clones + pushes per upstream
  (cached). For the full 500, prefer server-side fork + `create-ref` (no local
  transfer). First sync per fresh model is slow (~6 min: download + warmup).

## Run

```sh
# 0) one-time: fetch the pinned opencode binary (mounted into every container)
./fetch_opencode.sh

# 1) per-instance A/B (run_opencode.py — to build). CF creds come from ~/.zshrc;
#    needs OPENROUTER_API_KEY. NeoHive has no Auth0 → no PAT, CF token only.
OPENROUTER_API_KEY=... NEOHIVE_PROJECT=e873808e-5333-4641-9f25-c75bbc5744ff \
  ./.venv/bin/python run_opencode.py --arm a --model openrouter/z-ai/glm-4.6 psf__requests-1142
OPENROUTER_API_KEY=... NEOHIVE_PROJECT=e873808e-5333-4641-9f25-c75bbc5744ff \
  ./.venv/bin/python run_opencode.py --arm b --model openrouter/z-ai/glm-4.6 psf__requests-1142

# 2) grade either arm's preds.json (identical to Arm A)
./grade_swebench.sh results/<arm>-<...>/preds.json <run_id> 2
```
