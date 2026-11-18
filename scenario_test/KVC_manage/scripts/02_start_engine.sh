#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
TAG="${1:-default}"
ELOG="$ENGINE_LOG_DIR/engine-kvcmanage-$TAG-$(date +%m%d-%H%M%S).log"
say "restart engine $ENGINE_CONTAINER pool=$POOL_BLOCKS devices=$NPU_DEVICES"
docker restart "$ENGINE_CONTAINER" >/dev/null
sleep 3
docker exec -d -e ASCEND_RT_VISIBLE_DEVICES="$NPU_DEVICES" "$ENGINE_CONTAINER" bash -lc \
  "cd /vllm-workspace/vllm && nohup /usr/local/python3.12.13/bin/python3 /usr/local/python3.12.13/bin/vllm serve '$MODEL_PATH' --served-model-name '$MODEL_NAME' --port '$ENGINE_PORT' --tensor-parallel-size 2 --max-model-len 32768 --gpu-memory-utilization 0.9 --enforce-eager --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes --num-gpu-blocks-override '$POOL_BLOCKS' > '$ELOG' 2>&1"
H=000
for i in $(seq 1 120); do
  sleep 2
  H=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$ENGINE_PORT/health" 2>/dev/null || true)
  [ "$H" = "200" ] && break
done
say "engine health=$H log=$ELOG"
[ "$H" = "200" ] || { tail -40 "$ELOG" 2>/dev/null || true; exit 1; }
docker exec "$ENGINE_CONTAINER" sh -c "grep -a 'GPU KV cache size' '$ELOG' | tail -1" | sed 's/^/  /'
echo "$ELOG"
