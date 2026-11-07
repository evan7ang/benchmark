#!/usr/bin/env bash
# No-GPU Agent Hint CI:
#   1. dataset manifest
#   2. static hint/dataset validation
#   3. unit tests
#   4. mock send-side conformance (optional)
#
# It uses the local Python when it has datasets/pytest installed; otherwise it
# falls back to the ais-runner Docker image. Override with PYTHON=... or
# AIS_IMAGE=...
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
AIS_IMAGE="${AIS_IMAGE:-ais-runner:ais-1}"
DATA_DIR="${AGENT_HINT_DATA_DIR:-/home/t00887700/benchmark-data/agent_replay_v2}"
REPORT_DIR="${AGENT_HINT_CI_DIR:-$ROOT/outputs/agent-hint-ci}"
RUN_MOCK_CONFORMANCE="${RUN_MOCK_CONFORMANCE:-1}"
QUICK="${QUICK:-0}"
mkdir -p "$REPORT_DIR"

run_py() {
  if "$PYTHON" -c 'import datasets, pytest' >/dev/null 2>&1; then
    "$PYTHON" "$@"
    return
  fi
  if ! docker image inspect "$AIS_IMAGE" >/dev/null 2>&1; then
    echo "no usable Python deps and no $AIS_IMAGE image" >&2
    return 2
  fi
  local args=()
  local arg
  for arg in "$@"; do
    if [ "$arg" = "$ROOT" ]; then
      args+=("/workspace")
    elif [[ "$arg" == "$ROOT/"* ]]; then
      args+=("/workspace/${arg#"$ROOT/"}")
    else
      args+=("$arg")
    fi
  done
  local mounts=(-v "$ROOT":/workspace)
  if [ -d "$(dirname "$DATA_DIR")" ]; then
    mounts+=(-v "$(dirname "$DATA_DIR")":"$(dirname "$DATA_DIR")":ro)
  fi
  local local_dir="${AGENT_HINT_LOCAL_DATA_DIR:-/home/t00887700/kv-hint/.build/aisbench/data}"
  if [ -d "$local_dir" ]; then
    mounts+=(-v "$local_dir":"$local_dir":ro)
  fi
  docker run --rm --network host --entrypoint python3 \
    "${mounts[@]}" \
    -w /workspace \
    -e PYTHONPATH=/workspace \
    -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
    "$AIS_IMAGE" \
    "${args[@]}"
}

echo "[1/4] data manifest"
run_py "$ROOT/tests/agent_hint/data_manifest.py" \
  --required core \
  --report "$REPORT_DIR/data_manifest.json"

echo "[2/4] static dataset validation"
run_py "$ROOT/ais_bench/datasets/agent/validate_agent_hint_dataset.py" \
  "$ROOT/ais_bench/datasets/agent/example_v2.jsonl" \
  "$DATA_DIR/swebench_smoke_v2_running.jsonl" \
  "$DATA_DIR/swebench_smoke_v2_finalize.jsonl" \
  "$DATA_DIR/swebench_full50_v2_running.jsonl" \
  "$DATA_DIR/swebench_full50_v2_finalize.jsonl"

echo "[3/4] unit tests"
run_py -m pytest -q \
  "$ROOT/ais_bench/capture/tests" \
  "$ROOT/tests/UT/capture/test_capture_pipeline.py" \
  "$ROOT/tests/UT/utils/test_agent_hint.py" \
  "$ROOT/tests/UT/openicl/icl_inferencer/test_agent_hint_scheduling.py" \
  "$ROOT/tests/UT/openicl/icl_inferencer/test_icl_gen_inferencer.py" \
  "$ROOT/tests/UT/datasets/agent/test_build_agent_dataset_v2_pause.py" \
  "$ROOT/tests/UT/datasets/agent/test_validate_agent_hint_dataset.py"

if [ "$RUN_MOCK_CONFORMANCE" = "1" ]; then
  echo "[4/4] mock send-side conformance"
  if [ "$QUICK" = "1" ]; then
    AGENT_HINT_CONFORMANCE_DIR="$REPORT_DIR/conformance" \
      bash "$ROOT/tests/agent_hint/run_send_side_conformance.sh" --quick
  else
    AGENT_HINT_CONFORMANCE_DIR="$REPORT_DIR/conformance" \
      bash "$ROOT/tests/agent_hint/run_send_side_conformance.sh"
  fi
  echo "[report] mock ON/OFF report"
  run_py "$ROOT/tests/agent_hint/report_perf.py" \
    --on-run "$REPORT_DIR/conformance/on-example/run" \
    --off-run "$REPORT_DIR/conformance/off-example/run" \
    --out "$REPORT_DIR/conformance/report" \
    --title "Agent Hint CI mock ON/OFF"
else
  echo "[4/4] mock send-side conformance skipped (RUN_MOCK_CONFORMANCE=0)"
fi

echo "Agent Hint CI PASS"
