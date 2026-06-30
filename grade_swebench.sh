#!/usr/bin/env bash
# HIVE-264 — Grade generated patches against SWE-bench's hidden tests.
#
# Takes a mini-swe-agent run's preds.json and scores each model_patch inside the
# sealed per-instance container: applies the patch to a clean base_commit
# checkout, then runs the instance's FAIL_TO_PASS + PASS_TO_PASS suites. Emits a
# resolved/unresolved verdict per instance — the "grades end-to-end" half of the
# pilot (the run_swebench.sh rollout is the other half).
#
# Prereqs:
#   - .venv with `swebench` installed:  .venv/bin/pip install swebench
#   - Docker running (the x86_64 instance images are pulled on first use; the
#     rollout run usually cached them already).
#
# Usage:
#   ./grade_swebench.sh <preds.json> [run_id] [workers]
# Example:
#   ./grade_swebench.sh results/arma-..../preds.json glm52-armA-smoke 2
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/.venv/bin"

PREDS="${1:?usage: grade_swebench.sh <preds.json> [run_id] [workers]}"
RUN_ID="${2:-grade-$(basename "$(dirname "$PREDS")")}"
WORKERS="${3:-2}"

# The swebench harness talks to Docker via the Python docker SDK
# (docker.from_env()), which honors only DOCKER_HOST / the default
# /var/run/docker.sock — NOT the docker CLI's active *context*. On Colima (and
# any non-default daemon) the socket lives elsewhere, so without this the SDK
# dies at startup with "Error while fetching server API version:
# FileNotFoundError". Resolve the active context's endpoint and export it.
if [ -z "${DOCKER_HOST:-}" ]; then
  DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  export DOCKER_HOST
fi
echo "[grade] DOCKER_HOST=${DOCKER_HOST:-<default>}"
echo "[grade] preds=$PREDS run_id=$RUN_ID workers=$WORKERS"

# SWE-bench ships only x86_64 instance images; on Apple Silicon they run under
# emulation (correct, just slower). cache_level=instance keeps the pulled images
# so re-grades are fast. Reports land in results/ (gitignored).
exec "$VENV/python" -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-Bench_Verified \
  --split test \
  --predictions_path "$PREDS" \
  --run_id "$RUN_ID" \
  --max_workers "$WORKERS" \
  --cache_level instance \
  --namespace swebench \
  --report_dir "$HERE/results"
