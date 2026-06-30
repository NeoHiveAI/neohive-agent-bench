#!/usr/bin/env bash
# HIVE-264 — Run mini-swe-agent on the pinned pilot subset.
#
# Arm A (control, default) uses the stock swebench config unchanged — this is
# the baseline. Arm B (NeoHive-indexed repo) overlays a config built in HIVE-265.
#
# Prereqs:
#   - .venv with mini-swe-agent + datasets installed (see README).
#   - Docker running (per-instance images are pulled on first use; multi-GB).
#   - A model API key in the environment for the chosen provider, e.g.
#       OPENROUTER_API_KEY  (open models: deepseek / glm / kimi)
#       ANTHROPIC_API_KEY   (Claude Opus)
#       OPENAI_API_KEY      (GPT-5.x)
#
# Usage:
#   ./run_swebench.sh <litellm-model> [arm] [workers] [slice]
# Examples:
#   # cheap 5-instance smoke on an open model (~$0.50):
#   ./run_swebench.sh openrouter/deepseek/deepseek-chat a 4 0:5
#   # full 30-instance Arm-A pilot:
#   ./run_swebench.sh openrouter/deepseek/deepseek-chat a 8
set -euo pipefail
# Many newer OpenRouter models (glm-5.2, kimi, deepseek-v3.2, ...) aren't in
# LiteLLM's price map, which otherwise crashes the run during cost calc right
# after the first model response. We track cost ourselves (HIVE-273), so tell
# mini-swe-agent to ignore its internal cost-tracking errors. NOTE: this also
# neutralizes the config's per-instance cost_limit (cost reads as $0), so the
# step_limit (250) is the runaway guard.
export MSWEA_COST_TRACKING="${MSWEA_COST_TRACKING:-ignore_errors}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv/bin"

MODEL="${1:?usage: run_swebench.sh <litellm-model> [arm: a|b] [workers] [slice e.g. 0:5]}"
ARM="${2:-a}"
WORKERS="${3:-4}"
SLICE="${4:-}"

FILTER="$("$VENV/python" "$HERE/pilot_filter.py")"
SLUG="$(printf '%s' "$MODEL" | tr '/:.' '___')"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HERE/results/arm${ARM}-${SLUG}-${STAMP}"

ARGS=(--subset verified --split test --filter "$FILTER"
      -m "$MODEL" -w "$WORKERS" -o "$OUT" --environment-class docker)
[ -n "$SLICE" ] && ARGS+=(--slice "$SLICE")

if [ "$ARM" = "b" ]; then
  # Arm B = control + NeoHive (per-instance indexed repo + neohive-search). Built in HIVE-265.
  ARGS+=(-c swebench.yaml -c "$HERE/config/arm_b_neohive.yaml")
fi
# Arm A passes no -c, so the stock swebench.yaml default config is used unchanged.

echo "[run] arm=$ARM model=$MODEL workers=$WORKERS slice=${SLICE:-<all 30>}"
echo "[run] output -> $OUT"
exec "$VENV/mini-extra" swebench "${ARGS[@]}"
