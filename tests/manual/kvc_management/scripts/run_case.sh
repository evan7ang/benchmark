#!/usr/bin/env bash
# Run one AISBench case against the isolated KVC stack and collect evidence.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CASE=""
MODEL="kvc_manual_c8"
DATASET="kvc_manual_pause_offload_quick"
WARMUPS="0"
RUN_TIMEOUT=""

usage() {
    cat <<EOF
Usage: $0 --case NAME [options]
  --case NAME        evidence tag (ramp_c8, finalize_evict, ...)
  --model NAME       rendered AISBench model config name (default: $MODEL)
  --dataset NAME     rendered AISBench dataset config name (default: $DATASET)
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

python3 "$KIT_DIR/scripts/render_ais_configs.py" >/dev/null

RUN_NAME="$(date +%Y%m%d_%H%M%S)_${CASE}"
OUT="$RESULTS_DIR/$RUN_NAME"
mkdir -p "$OUT"

log "case=$CASE model=$MODEL dataset=$DATASET"
log "evidence: $OUT"

cat > "$OUT/meta.txt" <<EOF
case=$CASE
model=$MODEL
dataset=$DATASET
warmups=$WARMUPS
model_path=$MODEL_PATH
model_name=$MODEL_NAME
max_model_len=$MAX_MODEL_LEN
chat_api=$CHAT_API
engine_port=$ENGINE_PORT
motor_infer_port=$MOTOR_INFER_PORT
motor_mgmt_port=$MOTOR_MGMT_PORT
motor_obs_port=$MOTOR_OBS_PORT
card=$CARD
data_mode=$KVC_DATA_MODE
data_dir=$DATA_DIR
config_dir=$GENERATED_CONFIG_DIR
coord_log=$COORD_LOG
engine_log=$ENGINE_LOG
started=$(date -Iseconds)
EOF

curl -s --max-time 3 "http://${HOST_IP}:${MOTOR_OBS_PORT}/metrics" > "$OUT/motor-before.prom" || true
curl -s --max-time 3 "http://${HOST_IP}:${ENGINE_PORT}/metrics" > "$OUT/engine-before.prom" || true
ENGINE_LINES_BEFORE=$(wc -l < "$ENGINE_LOG" 2>/dev/null || echo 0)
COORD_LINES_BEFORE=$(wc -l < "$COORD_LOG" 2>/dev/null || echo 0)
printf 'engine=%s\ncoord=%s\n' "$ENGINE_LINES_BEFORE" "$COORD_LINES_BEFORE" > "$OUT/log-offsets.txt"

python3 "$KIT_DIR/scripts/sample_metrics.py" \
    --out "$OUT/engine-metrics.csv" \
    --engine-url "http://${HOST_IP}:${ENGINE_PORT}/metrics" \
    --motor-url "http://${HOST_IP}:${MOTOR_OBS_PORT}/metrics" \
    --interval 1 >"$OUT/sampler.out" 2>"$OUT/sampler.err" &
SAMPLER_PID=$!

cleanup_sampler() {
    kill "$SAMPLER_PID" >/dev/null 2>&1 || true
    wait "$SAMPLER_PID" >/dev/null 2>&1 || true
}
trap cleanup_sampler EXIT

AIS_CMD="ais_bench --config-dir '$GENERATED_CONFIG_DIR' \
  --models '$MODEL' \
  --datasets '$DATASET' \
  --mode perf --num-warmups '$WARMUPS' \
  --work-dir '$OUT/aisbench'"

RUN_RC=0
if [ "$AIS_RUN_MODE" = "host" ]; then
    log "running AISBench on host: $MODEL / $DATASET"
    set +e
    PYTHONPATH="$BENCH_DIR${PYTHONPATH:+:$PYTHONPATH}" TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      bash -lc "${AIS_PRE_CMD:+$AIS_PRE_CMD; }$AIS_CMD" >"$OUT/aisbench.log" 2>&1
    RUN_RC=$?
    set -e
else
    log "running AISBench in $AIS_IMAGE: $MODEL / $DATASET"
    AIS_NAME="${KVC_CONTAINER_PREFIX}-ais-$(date +%s)-$$"
    "$DOCKER_BIN" rm -f "$AIS_NAME" >/dev/null 2>&1 || true
    "$DOCKER_BIN" run -d --name "$AIS_NAME" --network host --entrypoint bash \
      -v "$BENCH_DIR:/workspace" \
      -v "$WORK_DIR:$WORK_DIR" \
      -v "$GENERATED_CONFIG_DIR:$GENERATED_CONFIG_DIR" \
      -v "$DATA_DIR:$DATA_DIR" \
      -v "$RESULTS_DIR:$RESULTS_DIR" \
      -v "$MODEL_PATH:$MODEL_PATH:ro" \
      -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      "$AIS_IMAGE" -lc "${AIS_PRE_CMD:+$AIS_PRE_CMD; }$AIS_CMD" >/dev/null
    START_TS=$(date +%s)
    while :; do
        RUNNING=$("$DOCKER_BIN" inspect -f '{{.State.Running}}' "$AIS_NAME" 2>/dev/null || echo false)
        [ "$RUNNING" = "true" ] || break
        if [ -n "$RUN_TIMEOUT" ]; then
            NOW_TS=$(date +%s)
            if [ $((NOW_TS - START_TS)) -ge "$RUN_TIMEOUT" ]; then
                log "duration ${RUN_TIMEOUT}s reached; stopping AISBench container"
                "$DOCKER_BIN" stop "$AIS_NAME" >/dev/null 2>&1 || true
                break
            fi
        fi
        sleep 2
    done
    "$DOCKER_BIN" logs "$AIS_NAME" >"$OUT/aisbench.log" 2>&1 || true
    RUN_RC=$("$DOCKER_BIN" inspect -f '{{.State.ExitCode}}' "$AIS_NAME" 2>/dev/null || echo -1)
    "$DOCKER_BIN" rm -f "$AIS_NAME" >/dev/null 2>&1 || true
fi

cleanup_sampler
trap - EXIT
echo "$RUN_RC" > "$OUT/run_rc.txt"
ENGINE_LINES_AFTER=$(wc -l < "$ENGINE_LOG" 2>/dev/null || echo 0)
COORD_LINES_AFTER=$(wc -l < "$COORD_LOG" 2>/dev/null || echo 0)
printf 'engine=%s\ncoord=%s\n' "$ENGINE_LINES_AFTER" "$COORD_LINES_AFTER" > "$OUT/log-offsets-end.txt"
log "AISBench exit code: $RUN_RC"

curl -s --max-time 3 "http://${HOST_IP}:${MOTOR_OBS_PORT}/metrics" > "$OUT/motor-after.prom" || true
curl -s --max-time 3 "http://${HOST_IP}:${ENGINE_PORT}/metrics" > "$OUT/engine-after.prom" || true

grep -E 'kvc (offload|prefetch|evict)|kvc manage dispatch|no session pin|central agent_status|released session pins|gate_skip' "$COORD_LOG" \
    > "$OUT/coordinator-kvc.log" 2>/dev/null || true
grep -E 'Executing edit|Context management result|classify_evict|kv_cache_usage_perc|Prefix cache' "$ENGINE_LOG" \
    > "$OUT/engine-kvc.log" 2>/dev/null || true

python3 "$KIT_DIR/scripts/check_case.py" \
    --case "$CASE" --out "$OUT" --engine-log "$ENGINE_LOG" --coord-log "$COORD_LOG" \
    | tee "$OUT/summary.txt" || true

printf '%s\n' "$OUT" > "$RESULTS_DIR/.last_result_dir"
log "done: $OUT"
[ "$RUN_RC" -eq 0 ] || exit "$RUN_RC"
