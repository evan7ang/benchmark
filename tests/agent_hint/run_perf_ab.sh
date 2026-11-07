#!/usr/bin/env bash
# Run one Agent Hint ON/OFF performance pair against an already-running Motor
# and engine, then build a compact report.
#
# Required: Motor must be reachable at AISBENCH_HOST_PORT and the dataset/models
# must exist. This script does not start Motor or vLLM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_ROOT="${AIS_WORK_ROOT:-$ROOT/outputs/agent-hint-perf/$STAMP}"
ON_MODEL="${ON_MODEL:-vllm_api_general_stream_session_hint_v2_trace}"
OFF_MODEL="${OFF_MODEL:-vllm_api_general_stream_session_hint_v2_trace_off}"
DATASET="${DATASET:-swebench_trace_v2}"
HOST_PORT="${AISBENCH_HOST_PORT:-19025}"
MOTOR_METRICS_URL="${MOTOR_METRICS_URL:-http://127.0.0.1:19027/metrics}"
AIS_IMAGE="${AIS_IMAGE:-ais-runner:ais-1}"
DATA_DIR="${AGENT_HINT_DATA_DIR:-/home/t00887700/benchmark-data/agent_replay_v2}"
LOCAL_DATA_DIR="${AGENT_HINT_LOCAL_DATA_DIR:-/home/t00887700/kv-hint/.build/aisbench/data}"
CAPTURE_DIR="${AGENT_HINT_CAPTURE_DIR:-/home/t00887700/benchmark-data/agent_capture_dsh/current}"

mkdir -p "$OUT_ROOT/on" "$OUT_ROOT/off"

MOUNTS=(-v "$ROOT":/workspace -v "$DATA_DIR":"$DATA_DIR":ro -v /home/RAG_data/models:/home/RAG_data/models:ro)
[ -d "$LOCAL_DATA_DIR" ] && MOUNTS+=(-v "$LOCAL_DATA_DIR":"$LOCAL_DATA_DIR":ro)
[ -d "$CAPTURE_DIR" ] && MOUNTS+=(-v "$CAPTURE_DIR":"$CAPTURE_DIR":ro)

run_arm() {
  local name="$1" model="$2" work="$3"
  echo "===== $name: $model / $DATASET ====="
  curl -sf "$MOTOR_METRICS_URL" > "$OUT_ROOT/$name/motor-before.prom" 2>/dev/null || true
  docker run --rm --network host --entrypoint bash \
    "${MOUNTS[@]}" \
    -w /workspace \
    -e PYTHONPATH=/workspace \
    -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
    -e AISBENCH_HOST_IP=127.0.0.1 \
    -e AISBENCH_HOST_PORT="$HOST_PORT" \
    -e AGENT_HINT_DATA_DIR="$DATA_DIR" \
    -e AGENT_HINT_LOCAL_DATA_DIR="$LOCAL_DATA_DIR" \
    -e AGENT_HINT_CAPTURE_DIR="$CAPTURE_DIR" \
    "$AIS_IMAGE" \
    -lc "pip uninstall -y av >/tmp/pip-av.log 2>&1; ais_bench --models $model --datasets $DATASET --mode perf --work-dir $work" \
    > "$OUT_ROOT/$name/aisbench.log" 2>&1
  curl -sf "$MOTOR_METRICS_URL" > "$OUT_ROOT/$name/motor-after.prom" 2>/dev/null || true
}

run_arm on "$ON_MODEL" "/workspace/outputs/agent-hint-perf/$STAMP/on/run"
run_arm off "$OFF_MODEL" "/workspace/outputs/agent-hint-perf/$STAMP/off/run"

python3 "$ROOT/tests/agent_hint/report_perf.py" \
  --on-run "$OUT_ROOT/on/run" \
  --off-run "$OUT_ROOT/off/run" \
  --motor-before "$OUT_ROOT/on/motor-before.prom" \
  --motor-after "$OUT_ROOT/on/motor-after.prom" \
  --out "$OUT_ROOT/report" \
  --title "Agent Hint ON/OFF: $DATASET"

echo "artifacts: $OUT_ROOT"
