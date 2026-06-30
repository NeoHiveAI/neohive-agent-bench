#!/usr/bin/env bash
# HIVE-265 + HIVE-268 — Run the Arm-B (NeoHive treatment) pilot.
#
# Arm B = Arm A's exact harness + NeoHive: each instance's repo is indexed at
# base_commit into its own NeoHive hive, and the agent gets a `neohive-search`
# command (the arm_b_neohive.yaml overlay) to retrieve over it.
#
# Unlike Arm A (one batch, one config), Arm B loops ONE instance at a time, because
# each instance needs its OWN hive scoped into the shim's env (NEOHIVE_HIVE). All
# instances still merge into a single preds.json, so grading is identical:
#   ./grade_swebench.sh results/armb-<model>-<stamp>/preds.json <run_id>
#
# Prereqs:
#   - .venv with mini-swe-agent + datasets (rollout) + swebench (grading).
#   - A REACHABLE NeoHive (host-side) and a PAT. Set:
#       NEOHIVE_BASE      host URL for the index API     (e.g. http://localhost:3577)
#       NEOHIVE_PROJECT   the project (HiveMind) id       (UUID)
#       NEOHIVE_PAT       a NeoHive PAT (pat_...)         (secret; never printed/committed)
#     The container reaches NeoHive at host.docker.internal:<port> (see arm_b overlay).
#   - Docker running (per-instance images pulled on first use).
#   - A model API key for the chosen provider (e.g. OPENROUTER_API_KEY).
#
# Usage:
#   ./run_arm_b.sh <litellm-model> [slice e.g. 0:5]
# Example (cheap 5-instance Arm-B smoke):
#   OPENROUTER_API_KEY=... NEOHIVE_BASE=http://localhost:3577 \
#   NEOHIVE_PROJECT=<uuid> NEOHIVE_PAT=pat_... \
#   ./run_arm_b.sh openrouter/z-ai/glm-5.2 0:5
set -euo pipefail
export MSWEA_COST_TRACKING="${MSWEA_COST_TRACKING:-ignore_errors}"  # see run_swebench.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv/bin"

MODEL="${1:?usage: run_arm_b.sh <litellm-model> [slice e.g. 0:5]}"
SLICE="${2:-}"

: "${NEOHIVE_BASE:?set NEOHIVE_BASE (host URL, e.g. http://localhost:3577)}"
: "${NEOHIVE_PROJECT:?set NEOHIVE_PROJECT (project/HiveMind id)}"
: "${NEOHIVE_PAT:?set NEOHIVE_PAT (a NeoHive PAT; never printed)}"

# Container-side MCP URL: the shim runs INSIDE the container and reaches the host
# NeoHive via host.docker.internal. Derive host:port from NEOHIVE_BASE.
PORT="$(printf '%s' "$NEOHIVE_BASE" | sed -E 's#^https?://[^:/]+:?([0-9]*).*#\1#')"
PORT="${PORT:-3577}"
SCHEME="$(printf '%s' "$NEOHIVE_BASE" | sed -E 's#^(https?)://.*#\1#')"
export NEOHIVE_MCP_URL="${SCHEME}://host.docker.internal:${PORT}/hiveminds/${NEOHIVE_PROJECT}/mcp"
export NEOHIVE_PAT

SLUG="$(printf '%s' "$MODEL" | tr '/:.' '___')"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HERE/results/armb-${SLUG}-${STAMP}"
mkdir -p "$OUT"

# Generate the per-run overlay with the absolute shim dir filled in (gitignored).
GEN_CFG="$OUT/arm_b_neohive.generated.yaml"
sed "s#__SHIM_DIR__#${HERE}#g" "$HERE/config/arm_b_neohive.yaml" > "$GEN_CFG"

# Ordered pilot instance ids, honoring the optional slice.
mapfile -t IDS < <("$VENV/python" - "$SLICE" <<'PY'
import json, sys
ids = [i["instance_id"] for i in json.load(open("pilot_subset.json"))["instances"]]
sl = sys.argv[1] if len(sys.argv) > 1 else ""
if sl:
    a = [int(x) if x else None for x in sl.split(":")]
    ids = ids[slice(*a)]
print("\n".join(ids))
PY
)

echo "[armb] model=$MODEL instances=${#IDS[@]} slice=${SLICE:-<all>}"
echo "[armb] output -> $OUT"
echo "[armb] container MCP url -> ${NEOHIVE_MCP_URL}"

for id in "${IDS[@]}"; do
  echo "=============================================================="
  echo "[armb] indexing $id ..."
  # index_instance.py prints 'NEOHIVE_HIVE=<id>' on success.
  HIVE_LINE="$("$VENV/python" "$HERE/index_instance.py" "$id" --workdir "$HERE/.localdata/armb" | tee /dev/stderr | grep '^NEOHIVE_HIVE=' || true)"
  if [ -z "$HIVE_LINE" ]; then
    echo "[armb] SKIP $id — indexing failed / no hive id" >&2
    continue
  fi
  export NEOHIVE_HIVE="${HIVE_LINE#NEOHIVE_HIVE=}"
  echo "[armb] running agent for $id (hive=$NEOHIVE_HIVE) ..."
  "$VENV/mini-extra" swebench \
    --subset verified --split test \
    --filter "^${id}$" \
    -m "$MODEL" -w 1 -o "$OUT" \
    --environment-class docker \
    -c swebench.yaml -c "$GEN_CFG" || echo "[armb] WARN: agent run failed for $id" >&2
done

echo "=============================================================="
echo "[armb] done. predictions -> $OUT/preds.json"
echo "[armb] grade with: ./grade_swebench.sh \"$OUT/preds.json\" armb-${SLUG}-${STAMP}"
