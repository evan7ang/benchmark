#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ARM="${1:?usage: 04_run_one.sh off|on}"
case "$ARM" in off) MODEL=lpstd_large_off;; on) MODEL=lpstd_large_on;; *) exit 2;; esac
OUT_NAME="$ARM-$(date +%m%d-%H%M%S)"
OUT="$ROOT/results/$OUT_NAME"
mkdir -p "$OUT"
say "run AISBench arm=$ARM model=$MODEL dataset=$DATASET"
[ -f "$DATASET" ] || { echo "missing dataset: $DATASET" >&2; exit 1; }
curl -s --max-time 3 "http://127.0.0.1:$MOTOR_OBS_PORT/metrics" > "$OUT/motor-before.prom" || true
curl -s --max-time 3 "http://127.0.0.1:$ENGINE_PORT/metrics" > "$OUT/engine-before.prom" || true
set +e
docker run --rm --network host --entrypoint bash \
  -v "$AIS_BENCH_DIR:/workspace" \
  -v "$ROOT:/workspace/tests/manual/long_pause_std" \
  -v "$MODEL_PATH:$MODEL_PATH:ro" \
  -e PYTHONPATH=/workspace \
  -e LPSTD_INFER_PORT="$MOTOR_INFER_PORT" \
  -e LPSTD_DATASET="/workspace/tests/manual/long_pause_std/data/generated/$(basename "$DATASET")" \
  "$AIS_IMAGE" -lc "pip uninstall -y av >/dev/null 2>&1 || true; cd /workspace && ais_bench --config-dir /workspace/tests/manual/long_pause_std/ais_bench/configs --models $MODEL --datasets lpstd_large --mode perf --num-warmups 0 --work-dir /workspace/tests/manual/long_pause_std/results/$OUT_NAME" \
  > "$OUT/aisbench.log" 2>&1
RC=$?
set -e
curl -s --max-time 3 "http://127.0.0.1:$MOTOR_OBS_PORT/metrics" > "$OUT/motor-after.prom" || true
curl -s --max-time 3 "http://127.0.0.1:$ENGINE_PORT/metrics" > "$OUT/engine-after.prom" || true
say "ais_bench arm=$ARM rc=$RC out=$OUT"
exit "$RC"
