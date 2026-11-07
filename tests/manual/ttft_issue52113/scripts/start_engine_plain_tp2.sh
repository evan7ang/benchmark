#!/usr/bin/env bash
# Start a plain vLLM engine (no Memcache connector, no Motor) for direct
# Agent Hint TTL tests. Default: TP2 on cards 2,3, port 18300.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CARDS="${CARDS:-2,3}"
TP="${TP:-2}"
ENGINE_PORT="${ENGINE_PORT:-18300}"
GMU="${GMU:-0.9}"
MAX_LEN="${MAX_LEN:-32768}"
ENGINE_NAME="${ENGINE_NAME:-ttft52113-engine-plain-tp${TP}}"
ENGINE_LOG="${ENGINE_LOG:-$KVC_LOG_DIR/engine-plain-tp${TP}-${ENGINE_PORT}.log}"
ENGINE_LOG_LEVEL="${ENGINE_LOG_LEVEL:-INFO}"
ENGINE_IMG="${ENGINE_IMG:-agenthint-vllm:0.1.0-p3memcache}"

if [ "${1:-start}" = "stop" ]; then
    docker rm -f "$ENGINE_NAME" >/dev/null 2>&1 || true
    echo "stopped $ENGINE_NAME"
    exit 0
fi

log "starting plain TP${TP} engine ($ENGINE_NAME) on cards $CARDS port $ENGINE_PORT"
for _ in 1 2 3; do docker rm -f "$ENGINE_NAME" >/dev/null 2>&1 && break; sleep 2; done

DEVICE_ARGS=()
IFS=',' read -r -a CARD_ARR <<< "$CARDS"
for card in "${CARD_ARR[@]}"; do
    DEVICE_ARGS+=(--device "/dev/davinci${card}")
done

docker run -d --name "$ENGINE_NAME" --privileged --network host --ipc private --shm-size 64g \
  --entrypoint bash \
  ${DEVICE_ARGS[@]+"${DEVICE_ARGS[@]}"} \
  --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
  -e ASCEND_RT_VISIBLE_DEVICES="$CARDS" -e ASCEND_RUNTIME_OPTIONS=NODRV \
  -e VLLM_ASCEND_ENABLE_AGENT_HINT=1 -e PYTHONHASHSEED=0 \
  -e VLLM_LOGGING_LEVEL="$ENGINE_LOG_LEVEL" \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v "$MODEL_PATH":"$MODEL_PATH" \
  -v "$KVC_LOG_DIR":"$KVC_LOG_DIR" \
  "$ENGINE_IMG" -c 'sleep infinity' >/dev/null
sleep 2

docker exec -d "$ENGINE_NAME" bash -lc "
  source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
  export ASCEND_RT_VISIBLE_DEVICES=$CARDS ASCEND_RUNTIME_OPTIONS=NODRV
  export PYTHONHASHSEED=0 VLLM_ASCEND_ENABLE_AGENT_HINT=1
  export VLLM_LOGGING_LEVEL=$ENGINE_LOG_LEVEL
  export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 VLLM_SERVER_DEV_MODE=1
  cd /vllm-workspace/vllm 2>/dev/null || true
  exec > '$ENGINE_LOG' 2>&1
  vllm serve '$MODEL_PATH' --served-model-name kvh --port '$ENGINE_PORT' \
    --tensor-parallel-size '$TP' --max-model-len '$MAX_LEN' --gpu-memory-utilization '$GMU' \
    --enforce-eager --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes
"

for _ in $(seq 1 600); do
    if curl -sf --max-time 2 "http://127.0.0.1:${ENGINE_PORT}/v1/models" >/dev/null 2>&1; then
        log "engine ready: http://127.0.0.1:${ENGINE_PORT} (TP${TP} cards=$CARDS)"
        grep -E "GPU KV cache size|Available KV cache memory" "$ENGINE_LOG" 2>/dev/null | tail -4 || true
        exit 0
    fi
    sleep 2
done
tail -80 "$ENGINE_LOG" >&2 || true
die "engine failed to become ready"
