# neohive-agent-bench

Harness for **Workstream 1** of the NeoHive benchmarking initiative — an A/B test
of whether an agent solves more real tasks with NeoHive wired in.

**Method:** per model, run two arms on SWE-bench:
- **Arm A (control)** — stock model + scaffold.
- **Arm B (treatment)** — identical, plus NeoHive with the instance's repo indexed
  at `base_commit` (gold patch + `test_patch` excluded). The agent retrieves over
  the codebase semantically instead of blind grep/read.

Pilot a ~30-instance subset to validate the methodology, then scale to full
SWE-bench Verified. Linear project: **Agent Benchmarking** (HIVE).

## Contents
| Path | Ticket | Purpose |
|---|---|---|
| `select_pilot_subset.py` | HIVE-288 | Reproducible stratified sampler (stdlib, no pip) |
| `pilot_subset.json` / `.md` | HIVE-288 | The pinned 30-instance pilot + distribution |
| `REACHABILITY_FINDINGS.md` | HIVE-263 | MCP reachability spike writeup |
| `mcp_reachability_probe.sh` + `mcp_roundtrip.py` | HIVE-263 | Runnable `memory_store→recall` probe |
| `grade_swebench.sh` | HIVE-264 | Grade a run's `preds.json` against the hidden tests (resolved/unresolved) |
| `ARM_B.md` | HIVE-265/268 | Arm-B (opencode + NeoHive MCP) design + status |
| `index_instance.py` | HIVE-268 | Clone repo @ `base_commit` → contamination guard → publish public mirror → index a per-instance code-embedding NeoHive hive |
| `fetch_opencode.sh` | HIVE-265 | Fetch the pinned opencode linux-x64 binary (mounted into every container) |
| `config/opencode-arm-a.json` / `-arm-b.json` | HIVE-265 | The two opencode configs; differ **only** by the `mcp.neohive` block |
| `run_opencode.py` *(to build)* | HIVE-265/268 | Per-instance, per-arm runner: opencode in-container → `git diff` → `preds.json` |
| `run_swebench.sh` + `pilot_filter.py` | HIVE-264 | Earlier mini-swe-agent harness used to validate the grading pipeline end-to-end (stepping stone; the A/B experiment uses opencode) |

## Scaffold: opencode (both arms)
The A/B uses **opencode** (model-agnostic via OpenRouter, MCP-native), run **inside**
each SWE-bench container so the agent has the repo's deps and can run tests. Arm A
and Arm B are the *same* opencode setup; the only difference is whether NeoHive is
connected as an MCP server (`config/opencode-arm-b.json`). The agent then uses
NeoHive's MCP tools (`memory_recall`, …) natively — exactly how a NeoHive end-user's
agent does. See `ARM_B.md`. (HIVE-264 first used mini-swe-agent to validate the
grading pipeline — that's `run_swebench.sh`, kept as a stepping stone.)

## Setup
```sh
python3 -m venv .venv
.venv/bin/pip install datasets swebench   # dataset access + grading (grade_swebench.sh)
./fetch_opencode.sh                       # pinned opencode binary for the containers
# (run_swebench.sh, the HIVE-264 validation harness, also needs: pip install mini-swe-agent)
```

## The A/B experiment → see `ARM_B.md`
Both arms run **opencode** in-container; Arm B adds NeoHive as an MCP server.
`index_instance.py` indexes each repo @ `base_commit`; `run_opencode.py` (to build)
runs an instance per arm; `grade_swebench.sh` scores the patches. Start there.

## Legacy: mini-swe-agent grading validation (HIVE-264)
`run_swebench.sh` ran the SWE-bench team's bash-only mini-swe-agent on the pinned 30
to validate the runner + grading pipeline end-to-end (graded 3/5 on a glm smoke). The
A/B experiment moved to opencode (real MCP usage); this is kept as a stepping stone.

```sh
# cheap 5-instance smoke on an open model (~$0.50):
OPENROUTER_API_KEY=... ./run_swebench.sh openrouter/deepseek/deepseek-chat a 4 0:5
# full 30-instance Arm-A pilot:
OPENROUTER_API_KEY=... ./run_swebench.sh openrouter/deepseek/deepseek-chat a 8
```

**Model strings (LiteLLM)** — verify against the current LiteLLM/OpenRouter model
list before pinning in HIVE-266:
- Open (OpenRouter): `openrouter/deepseek/deepseek-chat`, `openrouter/z-ai/glm-4.6`, `openrouter/moonshotai/kimi-k2.5`
- Frontier: `anthropic/claude-opus-4-8`, `openai/gpt-5.5`

**Needs to run:** a model API key for the chosen provider + Docker (per-instance
images are pulled on first use; multi-GB). The dataset+filter selection is
validated (it resolves to exactly the pinned 30); the model call + Docker run is
the only remaining step.

## Grade the patches (HIVE-264)
A run produces `preds.json` (`{instance_id, model_name_or_path, model_patch}`).
`grade_swebench.sh` scores those patches against SWE-bench's hidden tests inside
the sealed per-instance container and emits a resolved/unresolved verdict.

```sh
./grade_swebench.sh results/arma-<model>-<stamp>/preds.json glm52-armA-smoke 2
# report -> results/<model>.<run_id>.json  (resolved_ids / unresolved_ids)
```

Notes:
- **Colima / non-default Docker daemon:** the `swebench` harness uses the Python
  docker SDK (`docker.from_env()`), which reads `DOCKER_HOST` and ignores the
  docker CLI's active *context*. The script auto-exports `DOCKER_HOST` from the
  active context so it Just Works; without it the harness dies with
  "Error while fetching server API version: FileNotFoundError".
- **Apple Silicon:** SWE-bench ships only x86_64 instance images, so they run
  under emulation (correct, slower). For the full sweep, prefer a native x86_64
  host (see HIVE-264 / HIVE-176).

## Other utilities
```sh
python3 select_pilot_subset.py        # regenerate the pinned pilot subset (deterministic)

# reachability probe (needs a running NeoHive project + PAT):
export NEOHIVE_MCP_URL="http://localhost:3577/hiveminds/<project-id>/mcp"
export NEOHIVE_PAT="pat_..."
./mcp_reachability_probe.sh
```
