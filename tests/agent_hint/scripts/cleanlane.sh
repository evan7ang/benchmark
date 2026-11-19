#!/bin/bash
# Clean cold start of one engine instance, so every arm sees an empty prefix
# cache.  POST /reset_prefix_cache is not usable on this build (it answers
# {"success":false}), and a warm start hands the next arm a free ride: measured
# 99.4994 % vs 94.3862 % hit rate for the same trace, hot versus cold.
#
# usage: cleanlane.sh <port> <pool_blocks> <card[,card,...]>
# env:   MAXLEN             max model len passed to the engine (default 40960)
#        VLLM_BIN           engine binary          (default /usr/local/bin/vllm)
#        MODEL_PATH         weights directory
#        ENGINE_CONTAINER   container that hosts the engine process
#        ENGINE_LOGD        host dir for engine logs
#        SWEEP_HOOK         optional script run before launch (orphan cleanup)
set -u
PORT=${1:?port}
POOL=${2:?pool}
CARDS=${3:?cards}
MAXLEN=${MAXLEN:-40960}
VLLM=${VLLM_BIN:-/usr/local/bin/vllm}
MODEL=${MODEL_PATH:?MODEL_PATH}
C=${ENGINE_CONTAINER:?ENGINE_CONTAINER}
LOGD=${ENGINE_LOGD:-/tmp/engine-logs}
SWEEP_HOOK=${SWEEP_HOOK:-}
TS=$(date +%m%d-%H%M%S)
mkdir -p "$LOGD"

# Floor: one block holds 512/4 = 128 tokens; the engine refuses a pool that
# cannot cover max_model_len ("larger than the available KV cache").
FLOOR=$(( MAXLEN / 128 + 4 ))
if [ "$POOL" -lt "$FLOOR" ]; then
  echo "    pool $POOL below engine floor $FLOOR for maxlen $MAXLEN, raising"
  POOL=$FLOOR
fi

# --- 1. graceful stop --------------------------------------------------------
# SIGTERM first: SIGKILL leaves EngineCore alive still holding the HBM.
pids=$(ps -eo pid,args | grep "vllm serve" | grep -- "--port $PORT " | grep -v grep | awk '{print $1}')
if [ -n "$pids" ]; then
  echo "SIGTERM api servers: $pids"
  kill -TERM $pids 2>/dev/null
fi
for i in $(seq 1 60); do
  left=$(ps -eo pid,args | grep "vllm serve" | grep -- "--port $PORT " | grep -v grep | wc -l)
  [ "$left" = "0" ] && break
  sleep 2
done
left=$(ps -eo pid,args | grep "vllm serve" | grep -- "--port $PORT " | grep -v grep | wc -l)
if [ "$left" != "0" ]; then
  echo "still alive after 120s -> SIGKILL"
  pkill -9 -f "vllm serve.*--port $PORT" 2>/dev/null
  sleep 5
fi
docker exec $C bash -lc "pkill -9 -f 'vllm serve.*--port $PORT'" 2>/dev/null
sleep 3

# --- 2. optional orphan cleanup ---------------------------------------------
if [ -n "$SWEEP_HOOK" ] && [ -x "$SWEEP_HOOK" ]; then
  bash "$SWEEP_HOOK" 2>&1 | sed 's/^/    /'
fi

# --- 3. launch on the first card that has room -------------------------------
IFS=',' read -ra LIST <<< "$CARDS"
for DEV in "${LIST[@]}"; do
  for attempt in 1 2; do
    LOG=$LOGD/engine-port${PORT}-card${DEV}-pool${POOL}-$TS-a${attempt}.log
    echo "--- card $DEV attempt $attempt (pool $POOL) -> $(basename "$LOG")"
    setsid nohup docker exec $C bash -lc "export ASCEND_RT_VISIBLE_DEVICES=$DEV; cd /vllm-workspace/vllm && exec $VLLM serve $MODEL --served-model-name ${SERVED_MODEL:-kvh} --port $PORT --tensor-parallel-size 1 --max-model-len $MAXLEN --gpu-memory-utilization 0.9 --enforce-eager --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes --num-gpu-blocks-override $POOL" > "$LOG" 2>&1 &
    ok=0
    for i in $(seq 1 100); do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$PORT/health 2>/dev/null)
      [ "$code" = "200" ] && { ok=1; break; }
      if grep -q 'is less than desired GPU memory utilization' "$LOG" 2>/dev/null; then echo "    card $DEV insufficient HBM"; break; fi
      if grep -qE 'NPU out of memory|RuntimeError: Engine core initialization failed|larger than the available KV cache' "$LOG" 2>/dev/null; then echo "    card $DEV init failed"; attempt=9; break; fi
      sleep 5
    done
    if [ "$ok" = "1" ]; then
      blocks=$(curl -s --max-time 5 http://127.0.0.1:$PORT/metrics 2>/dev/null | grep -o 'num_gpu_blocks="[0-9]*"' | head -1)
      echo "    HEALTHY card $DEV $blocks"
      exit 0
    fi
    echo "    card $DEV attempt $attempt: no /health in 500s, cleaning up"
    pkill -9 -f "vllm serve.*--port $PORT" 2>/dev/null
    docker exec $C bash -lc "pkill -9 -f 'vllm serve.*--port $PORT'" 2>/dev/null
    sleep 3
    [ -n "$SWEEP_HOOK" ] && [ -x "$SWEEP_HOOK" ] && bash "$SWEEP_HOOK" >/dev/null 2>&1
    [ "$attempt" = "9" ] && break
    sleep 5
  done
done
echo "NO CARD AVAILABLE for $PORT"
exit 2