#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
say "preflight"
test -e "$MODEL_PATH" || { echo "missing model: $MODEL_PATH" >&2; exit 1; }
test -d "$AIS_BENCH_DIR" || { echo "missing AISBench: $AIS_BENCH_DIR" >&2; exit 1; }
docker image inspect "$ENGINE_IMAGE" >/dev/null 2>&1 || { echo "missing image $ENGINE_IMAGE" >&2; exit 1; }
docker image inspect "$MOTOR_IMAGE" >/dev/null 2>&1 || { echo "missing image $MOTOR_IMAGE" >&2; exit 1; }
docker image inspect "$AIS_IMAGE" >/dev/null 2>&1 || { echo "missing image $AIS_IMAGE" >&2; exit 1; }
co=$(docker inspect -f '{{.State.Status}}' "$ENGINE_CONTAINER" 2>/dev/null || true)
mo=$(docker inspect -f '{{.State.Status}}' "$MOTOR_CONTAINER" 2>/dev/null || true)
say "engine=$ENGINE_CONTAINER($co) motor=$MOTOR_CONTAINER($mo) "
say "npu=$NPU_DEVICES pool=$POOL_BLOCKS dataset=$DATASET"
[ -f "$DATASET" ] || say "WARN dataset missing; run data/gen_lpstd.py"
echo OK
