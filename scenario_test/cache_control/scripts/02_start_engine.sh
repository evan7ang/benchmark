#!/bin/bash
# 02 起引擎（或在已健康时校验 banner），并把池子块数写进 results/logs/pool.env
#   用法: 02_start_engine.sh [--force]
#   POOL_BLOCKS_OVERRIDE 非空时校验 banner == override*128（容量实验的唯一旋钮）
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

FORCE=0
case "${1:-}" in --force|--restart) FORCE=1;; esac

guard_container "$ENGINE_CONTAINER"
require_container "$ENGINE_CONTAINER"
LOG="$LOG_DIR/engine-$ENGINE_PORT.log"
POOLENV="$LOG_DIR/pool.env"

read_banner() { # -> "tokens blocks"
  local line tok blk
  line=$(grep -a 'GPU KV cache size' "$LOG" 2>/dev/null | tail -1 || true)
  if [ -n "$line" ]; then
    tok=$(echo "$line" | sed -n 's/.*size: *\([0-9,]*\) *tokens.*/\1/p' | tr -d ',')
    if [ -n "$tok" ]; then
      blk=$(( tok / 128 ))
      echo "$tok $blk"
      return
    fi
  fi
  # 兜底：日志不可用（复用已在跑的引擎）时，从 /metrics 的 cache_config_info 读
  local m
  m=$(curl -s --max-time 5 "http://127.0.0.1:${ENGINE_PORT}/metrics" 2>/dev/null \
      | grep -a 'vllm:cache_config_info' | tail -1 || true)
  if [ -n "$m" ]; then
    tok=$(echo "$m" | sed -n 's/.*kv_cache_size_tokens="\([0-9]*\)".*/\1/p')
    blk=$(echo "$m" | sed -n 's/.*num_gpu_blocks="\([0-9]*\)".*/\1/p')
    if [ -n "$tok" ] && [ -n "$blk" ]; then
      echo "$tok $blk"
      return
    fi
  fi
  echo ""
}

if [ "$FORCE" -eq 0 ] && [ "$(http "http://127.0.0.1:$ENGINE_PORT/health")" = "200" ]; then
  read -r TOK BLK <<<"$(read_banner)"
  if [ -n "${TOK:-}" ]; then
    say "引擎已健康（:${ENGINE_PORT}），banner: ${TOK} tokens = ${BLK} blocks"
  else
    say "引擎已健康（:${ENGINE_PORT}），但日志无 banner（$LOG）；沿用 env 的 override 或请 --force 重启"
    TOK=""; BLK="${POOL_BLOCKS_OVERRIDE:-}"
  fi
else
  say "重启引擎进程（容器 $ENGINE_CONTAINER, NPU=$NPU_INDEX, port=$ENGINE_PORT）"
  docker exec "$ENGINE_CONTAINER" sh -c "pkill -f 'vllm serve' 2>/dev/null || true" >/dev/null 2>&1 || true
  sleep 3
  : > "$LOG"
  OVERRIDE_ARG=""
  [ -n "${POOL_BLOCKS_OVERRIDE:-}" ] && OVERRIDE_ARG="--num-gpu-blocks-override ${POOL_BLOCKS_OVERRIDE}"
  docker exec -d "$ENGINE_CONTAINER" bash -lc "
    source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
    export ASCEND_RT_VISIBLE_DEVICES=$NPU_INDEX ASCEND_RUNTIME_OPTIONS=NODRV
    export PYTHONHASHSEED=0 VLLM_ASCEND_ENABLE_AGENT_HINT=1 VLLM_LOGGING_LEVEL=INFO
    export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
    export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 VLLM_SERVER_DEV_MODE=1
    export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=3600 VLLM_RINGBUFFER_WARNING_INTERVAL=600
    export MASTER_PORT=$MASTER_PORT
    cd /vllm-workspace/vllm 2>/dev/null || true
    exec > $LOG 2>&1
    vllm serve $MODEL_PATH --served-model-name $SERVED_MODEL_NAME --port $ENGINE_PORT \
      --tensor-parallel-size 1 --max-model-len 32768 --gpu-memory-utilization 0.9 \
      --enforce-eager --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes \
      $OVERRIDE_ARG
  "
  say "等待引擎健康（最长 10 分钟）..."
  wait_until_http "http://127.0.0.1:${ENGINE_PORT}/health" 200 600 || die "引擎未就绪，见 $LOG"
  sleep 2
  read -r TOK BLK <<<"$(read_banner)"
  [ -n "${TOK:-}" ] || die "日志中没有 'GPU KV cache size'，见 $LOG"
  say "banner: ${TOK} tokens = ${BLK} blocks"
fi

if [ -n "${POOL_BLOCKS_OVERRIDE:-}" ] && [ -n "${BLK:-}" ]; then
  WANT=$(( POOL_BLOCKS_OVERRIDE * 128 ))
  if [ "${TOK:-0}" != "$WANT" ]; then
    die "池子不符：期望 ${WANT} tokens（override=${POOL_BLOCKS_OVERRIDE}），实际 ${TOK:-?}。该轮作废。"
  fi
  say "池子校验通过：override=${POOL_BLOCKS_OVERRIDE} 块"
fi

{
  echo "POOL_TOKENS=${TOK:-}"
  echo "POOL_BLOCKS=${BLK:-}"
} > "$POOLENV"
say "已写 $POOLENV"

EPID="$(engine_pid)"
ESTART="$(engine_started "$EPID")"
write_engine_epoch "$EPID" "$ESTART" "${TOK:-}" "${BLK:-}"
say "引擎纪元: pid=${EPID:-?} 启动=${ESTART:-?}（写入 $ENGINE_EPOCH）"

# 新引擎实例 => 清空该实例的臂历史标记（历史文件按 pid 区分，无需删除）
[ -n "${EPID:-}" ] && say "提示：本轮之后若切臂，请用 02_start_engine.sh --restart 重建引擎，避免 hint 流量污染 off 基线。"
