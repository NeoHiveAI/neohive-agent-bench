# HIVE-263 — NeoHive MCP reachability from the SWE-bench eval container

**Status: the gate is largely de-risked by design.** NeoHive is only needed
during the *agent/inference* phase, which has network egress; the *grading*
phase runs in a sealed, network-disabled container and never touches NeoHive.

## The key distinction: two phases
SWE-bench runs in two separate phases, and they have opposite network postures:

| Phase | What happens | Network | NeoHive |
|---|---|---|---|
| **Agent / inference** | Scaffold drives the model + tools to produce a patch (the *prediction*) | **ON** — must reach the model API + install deps | **Used here** (Arm B retrieves over the indexed repo) |
| **Grading** | Official harness applies the predicted patch in a *fresh, sealed, network-disabled* Docker image and runs the hidden tests | **OFF** | **Not involved** — and must not be reachable (it isn't) |

Consequence: NeoHive is never reachable at grade time (no contamination via
NeoHive at scoring), and reachability is purely an *agent-phase* concern.

## Two scaffold patterns → two reachability stories
1. **Host-side agent (SWE-agent, mini-swe-agent default) — recommended.** The
   agent loop + MCP/LLM clients run on the **host**; the per-instance container
   is only an execution sandbox for bash/edit/test. The MCP client sits next to
   the LLM client, so NeoHive at `http://localhost:3577/hiveminds/<id>/mcp` is
   **directly reachable. Reachability is essentially free** — no container
   networking work.
2. **In-container agent (agent CLI inside the container).** The MCP client runs
   **inside** the per-instance container and needs egress to the host NeoHive:
   - **`--network host`** (Linux): container shares host net → `localhost:3577` works.
   - **bridge** (Docker Desktop mac/win): use `host.docker.internal:3577`; on
     Linux add `--add-host=host.docker.internal:host-gateway`.
   - **Auth:** pass `NEOHIVE_PAT` into the container env; the gateway auth
     middleware accepts a `pat_…` token (try `Authorization: Bearer`, fall back
     to `x-api-key`).
   - **TLS:** NeoHive serves plain HTTP on :3577 → no TLS needed. If a client
     requires stdio, wrap with `npx mcp-remote <url>` (stdio↔HTTP bridge).

## Recommendation
Adopt **Pattern 1** (host-side agent) in HIVE-264 → reachability is trivial.
The probe deliberately tests **Pattern 2** (the harder, in-container case) so
we are covered either way.

## Acceptance test (this dir)
`mcp_reachability_probe.sh` runs `mcp_roundtrip.py` from inside a Docker
container against an external NeoHive and does a real
`memory_store → memory_recall` round-trip, printing per-call latency. It tries
both `--network host` and `bridge + host.docker.internal`.

```sh
export NEOHIVE_MCP_URL="http://localhost:3577/hiveminds/<project-id>/mcp"
export NEOHIVE_PAT="pat_..."
./mcp_reachability_probe.sh
```

Docker is available on the dev box (v29.6.0). The probe needs a **running
NeoHive project + PAT** to complete the round-trip — run it against the dev
instance to flip the gate green.

## Open items (hand-off)
- **Confirm the scaffold (HIVE-264).** If we pick a host-side agent (Pattern 1),
  Pattern 2 networking is never exercised and the gate is closed trivially.
- **Validate transport/auth specifics on first run.** `mcp_roundtrip.py` assumes
  streamable-HTTP + `Authorization: Bearer <pat>`; if the gateway 401s, switch
  to `x-api-key` (one-line toggle in the script).
- **Egress allowlist.** If we run in-container with restricted egress, ensure the
  NeoHive host shares the same allowlist as the model-API host (enabling one
  enables the other).

## Sources
- SWE-bench harness (sealed per-instance Docker, offline grading): <https://github.com/swe-bench/SWE-bench>, <https://www.swebench.com/SWE-bench/>
- NeoHive serves plain HTTP on :3577; wrap with `mcp-remote` for stdio clients (NeoHive installer/docs).
