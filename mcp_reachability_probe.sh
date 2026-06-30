#!/usr/bin/env bash
# HIVE-263 — Prove NeoHive MCP reachability from inside a container.
#
# Runs mcp_roundtrip.py from inside a Docker container (mimicking the SWE-bench
# per-instance agent container) against an EXTERNAL NeoHive, doing a real
# memory_store -> memory_recall round-trip. Tries the two networking modes that
# matter for the in-container ("Pattern 2") case. See REACHABILITY_FINDINGS.md.
#
# Prereqs: a running NeoHive with a project, and a PAT.
#   export NEOHIVE_MCP_URL="http://localhost:3577/hiveminds/<project-id>/mcp"
#   export NEOHIVE_PAT="pat_..."
#   ./mcp_reachability_probe.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="python:3.12-slim"
: "${NEOHIVE_MCP_URL:?set NEOHIVE_MCP_URL (e.g. http://localhost:3577/hiveminds/<id>/mcp)}"
: "${NEOHIVE_PAT:?set NEOHIVE_PAT}"

# Rewrite localhost/127.0.0.1 -> host.docker.internal for the bridge-network mode.
BRIDGE_URL="${NEOHIVE_MCP_URL/localhost/host.docker.internal}"
BRIDGE_URL="${BRIDGE_URL/127.0.0.1/host.docker.internal}"

run() {  # $1=label  $2..=extra docker args ; NEOHIVE_MCP_URL passed via env
  local label="$1"; shift
  echo "================================================================"
  echo "  MODE: $label"
  echo "================================================================"
  if docker run --rm \
      -e NEOHIVE_MCP_URL -e NEOHIVE_PAT \
      -v "$HERE/mcp_roundtrip.py:/probe/mcp_roundtrip.py:ro" \
      "$@" "$IMAGE" python /probe/mcp_roundtrip.py; then
    echo "  -> $label: PASS"
    return 0
  else
    echo "  -> $label: FAIL"
    return 1
  fi
}

PASS=0
# Mode A: --network host (Linux). Container shares host net; localhost works.
if NEOHIVE_MCP_URL="$NEOHIVE_MCP_URL" run "host network (localhost)" --network host; then
  PASS=1
fi
# Mode B: default bridge + host.docker.internal (Docker Desktop mac/win; Linux needs host-gateway).
if NEOHIVE_MCP_URL="$BRIDGE_URL" run "bridge + host.docker.internal" \
     --add-host=host.docker.internal:host-gateway; then
  PASS=1
fi

echo "================================================================"
if [ "$PASS" -eq 1 ]; then
  echo "GATE GREEN: at least one in-container mode reached NeoHive end-to-end."
  echo "(Host-side agent scaffolds reach NeoHive at localhost directly — strictly easier.)"
  exit 0
else
  echo "GATE RED: no mode completed the round-trip. Check that NeoHive is running,"
  echo "the project MCP URL + PAT are correct, and host egress is allowed."
  exit 1
fi
