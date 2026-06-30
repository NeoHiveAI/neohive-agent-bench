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

## Quickstart
```sh
python3 select_pilot_subset.py        # regenerate the pinned pilot subset (deterministic)

# reachability probe (needs a running NeoHive project + PAT):
export NEOHIVE_MCP_URL="http://localhost:3577/hiveminds/<project-id>/mcp"
export NEOHIVE_PAT="pat_..."
./mcp_reachability_probe.sh
```
