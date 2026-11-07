#!/usr/bin/env bash
# Run one AISBench case against the isolated KVC stack and collect evidence.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CASE=""
MODEL="ttft52113_on_c5"
DATASET="ttft52113_running"
WARMUPS="0"
RUN_TIMEOUT=""

usage() {
    cat <<EOF
Usage: $0 --case NAME [options]
  --case NAME        evidence tag (ramp_c8, finalize_evict, pause_offload, ...)
  --model NAME       AISBench model config name (default: $MODEL)
  --dataset NAME     AISBench dataset config name (default: $DATASET)
  --warmups N        num warmups (default: 0)
  --timeout SEC      optional duration limit; the AISBench container is stopped afterwards
  --duration SEC     alias for --timeout
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --case) CASE="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --dataset) DATASET="$2"; shift 2 ;;
        --warmups) WARMUPS="$2"; shift 2 ;;
        --timeout|--duration) RUN_TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done
[ -n "$CASE" ] || die "--case is required"

RUN_NAME="$(date +%Y%m%d_%H%M%S)_${CASE}"
OUT="$RESULTS_DIR/$RUN_NAME"
mkdir -p "$OUT"

REL_KIT="${KIT_DIR#"$BENCH_DIR"/}"
CONTAINER_KIT="/workspace/$REL_KIT"
CONTAINER_OUT="$CONTAINER_KIT/results/$RUN_NAME/aisbench"

log "case=$CASE model=$MODEL dataset=$DATASET"
log "evidence: $OUT"

cat > "$OUT/meta.txt" <<EOF
case=$CASE
model=$MODEL
dataset=$DATASET
warmups=$WARMUPS
engine_port=$ENGINE_PORT
motor_infer_port=$MOTOR_INFER_PORT
motor_mgmt_port=$MOTOR_MGMT_PORT
motor_obs_port=$MOTOR_OBS_PORT
card=$CARD
coord_log=$COORD_LOG
engine_log=$ENGINE_LOG
started=$(date -Iseconds)
EOF

curl -s --max-time 3 "http://127.0.0.1:${MOTOR_OBS_PORT}/metrics" > "$OUT/motor-before.prom" || true
curl -s --max-time 3 "http://127.0.0.1:${ENGINE_PORT}/metrics" > "$OUT/engine-before.prom" || true
ENGINE_LINES_BEFORE=$(wc -l < "$ENGINE_LOG" 2>/dev/null || echo 0)
COORD_LINES_BEFORE=$(wc -l < "$COORD_LOG" 2>/dev/null || echo 0)
printf 'engine=%s\ncoord=%s\n' "$ENGINE_LINES_BEFORE" "$COORD_LINES_BEFORE" > "$OUT/log-offsets.txt"

python3 "$KIT_DIR/scripts/sample_metrics.py" \
    --out "$OUT/engine-metrics.csv" \
    --engine-url "http://127.0.0.1:${ENGINE_PORT}/metrics" \
    --motor-url "http://127.0.0.1:${MOTOR_OBS_PORT}/metrics" \
    --interval 1 >"$OUT/sampler.out" 2>"$OUT/sampler.err" &
SAMPLER_PID=$!

cleanup_sampler() {
    kill "$SAMPLER_PID" >/dev/null 2>&1 || true
    wait "$SAMPLER_PID" >/dev/null 2>&1 || true
}
trap cleanup_sampler EXIT

AIS_CMD="ais_bench --config-dir '$CONTAINER_KIT/configs' \
  --models '$MODEL' \
  --datasets '$DATASET' \
  --mode perf --num-warmups '$WARMUPS' \
  --work-dir '$CONTAINER_OUT'"

log "running AISBench: $MODEL / $DATASET"
AIS_NAME="ttft52113-ais-$(date +%s)-$$"
docker rm -f "$AIS_NAME" >/dev/null 2>&1 || true
docker run -d --name "$AIS_NAME" --network host --entrypoint bash \
  -v "$BENCH_DIR:/workspace" \
  -v "/home/t00887700/benchmark-data:/home/t00887700/benchmark-data:ro" \
  -v "$MODEL_PATH:$MODEL_PATH:ro" \
  -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
  "$AIS_IMG" -lc "pip uninstall -y av >/dev/null 2>&1; $AIS_CMD" >/dev/null
START_TS=$(date +%s)
while :; do
    RUNNING=$(docker inspect -f '{{.State.Running}}' "$AIS_NAME" 2>/dev/null || echo false)
    [ "$RUNNING" = "true" ] || break
    if [ -n "$RUN_TIMEOUT" ]; then
        NOW_TS=$(date +%s)
        if [ $((NOW_TS - START_TS)) -ge "$RUN_TIMEOUT" ]; then
            log "duration ${RUN_TIMEOUT}s reached; stopping AISBench container"
            docker stop "$AIS_NAME" >/dev/null 2>&1 || true
            break
        fi
    fi
    sleep 2
done
docker logs "$AIS_NAME" >"$OUT/aisbench.log" 2>&1 || true
RUN_RC=$(docker inspect -f '{{.State.ExitCode}}' "$AIS_NAME" 2>/dev/null || echo -1)
docker rm -f "$AIS_NAME" >/dev/null 2>&1 || true
cleanup_sampler
trap - EXIT
echo "$RUN_RC" > "$OUT/run_rc.txt"
log "AISBench exit code: $RUN_RC"

curl -s --max-time 3 "http://127.0.0.1:${MOTOR_OBS_PORT}/metrics" > "$OUT/motor-after.prom" || true
curl -s --max-time 3 "http://127.0.0.1:${ENGINE_PORT}/metrics" > "$OUT/engine-after.prom" || true

grep -E 'kvc (offload|prefetch|evict)|kvc manage dispatch|no session pin|central agent_status|released session pins|gate_skip' "$COORD_LOG" \
    > "$OUT/coordinator-kvc.log" 2>/dev/null || true
grep -E 'Executing edit|Context management result|classify_evict|kv_cache_usage_perc|Prefix cache' "$ENGINE_LOG" \
    > "$OUT/engine-kvc.log" 2>/dev/null || true
if [ -f "$ENGINE_LOG" ]; then
    cp "$ENGINE_LOG" "$OUT/engine-full.log" 2>/dev/null || true
fi

python3 "$KIT_DIR/scripts/check_case.py" \
    --case "$CASE" --out "$OUT" --engine-log "$ENGINE_LOG" --coord-log "$COORD_LOG" \
    | tee "$OUT/summary.txt" || true

log "done: $OUT"
[ "$RUN_RC" -eq 0 ] || exit "$RUN_RC"
