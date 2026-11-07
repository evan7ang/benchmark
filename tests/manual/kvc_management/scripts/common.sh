#!/usr/bin/env bash
# Shared configuration loader for the KVC manual test kit.
#
# Precedence:
#   explicit environment > KVC_TEST_ENV file > ./kvc_test.env > built-in defaults
#
# The kit never requires the original developer machine layout: every path,
# image, port, card, and model name comes from this configuration.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR=""

# Locate the config file without clobbering explicitly exported variables.
KVC_ENV_CANDIDATE="${KVC_TEST_ENV:-}"
if [ -z "$KVC_ENV_CANDIDATE" ] && [ -f "$KIT_DIR/kvc_test.env" ]; then
    KVC_ENV_CANDIDATE="$KIT_DIR/kvc_test.env"
fi
if [ -z "$KVC_ENV_CANDIDATE" ] && [ -f "$PWD/kvc_test.env" ]; then
    KVC_ENV_CANDIDATE="$PWD/kvc_test.env"
fi
if [ -n "$KVC_ENV_CANDIDATE" ] && [ -f "$KVC_ENV_CANDIDATE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$KVC_ENV_CANDIDATE"
    set +a
    KVC_TEST_ENV_FILE="$KVC_ENV_CANDIDATE"
fi

# Resolve the benchmark source: explicit config > kit inside benchmark > cwd.
if [ -n "${BENCH_SOURCE_DIR:-}" ]; then
    BENCH_DIR="$BENCH_SOURCE_DIR"
elif [ -d "$KIT_DIR/../../../ais_bench" ]; then
    BENCH_DIR="$(cd "$KIT_DIR/../../.." && pwd)"
elif [ -d "$PWD/ais_bench" ]; then
    BENCH_DIR="$PWD"
else
    BENCH_DIR="$KIT_DIR"
fi

# Model
MODEL_PATH="${MODEL_PATH:-}"
TOKENIZER_PATH="${TOKENIZER_PATH:-$MODEL_PATH}"
MODEL_NAME="${MODEL_NAME:-kvh}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
CHAT_API="${CHAT_API:-true}"
TP_SIZE="${TP_SIZE:-1}"
CARD="${CARD:-0}"
CARDS="${CARDS:-$CARD}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.85}"
ENGINE_START_TIMEOUT="${ENGINE_START_TIMEOUT:-300}"

# Images
ENGINE_IMAGE="${ENGINE_IMAGE:-agenthint-vllm:0.1.0-p3memcache}"
MOTOR_IMAGE="${MOTOR_IMAGE:-mindie-motor-vllm:e2e3b}"
AIS_IMAGE="${AIS_IMAGE:-ais-runner:ais-1}"

# Motor installation
MOTOR_INSTALL_MODE="${MOTOR_INSTALL_MODE:-image}"
MOTOR_WHEEL="${MOTOR_WHEEL:-}"
MOTOR_SOURCE_DIR="${MOTOR_SOURCE_DIR:-}"

# Paths
WORK_DIR="${WORK_DIR:-$KIT_DIR/work}"
DATA_DIR="${DATA_DIR:-$WORK_DIR/data}"
RESULTS_DIR="${RESULTS_DIR:-$WORK_DIR/results}"
GENERATED_CONFIG_DIR="${GENERATED_CONFIG_DIR:-$WORK_DIR/generated_configs}"
KVC_LOG_DIR="${KVC_LOG_DIR:-$WORK_DIR/logs}"
KVC_MMC_DIR="${KVC_MMC_DIR:-$WORK_DIR/memcache}"
KVC_TEST_ENV_FILE="${KVC_TEST_ENV_FILE:-}"

# Ports
ENGINE_PORT="${ENGINE_PORT:-18000}"
MOTOR_INFER_PORT="${MOTOR_INFER_PORT:-19125}"
MOTOR_MGMT_PORT="${MOTOR_MGMT_PORT:-19126}"
MOTOR_OBS_PORT="${MOTOR_OBS_PORT:-19127}"
MMC_META_PORT="${MMC_META_PORT:-15000}"
MMC_STORE_PORT="${MMC_STORE_PORT:-16000}"
MMC_METRICS_PORT="${MMC_METRICS_PORT:-18181}"
MMC_LOOKUP_PORT="${MMC_LOOKUP_PORT:-19100}"

# Runtime
HOST_IP="${HOST_IP:-127.0.0.1}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
KVC_CONTAINER_PREFIX="${KVC_CONTAINER_PREFIX:-kvcfunc}"
AIS_RUN_MODE="${AIS_RUN_MODE:-docker}"
AIS_PRE_CMD="${AIS_PRE_CMD:-pip uninstall -y av >/dev/null 2>&1 || true}"
ENGINE_TOOL_ARGS="${ENGINE_TOOL_ARGS---enable-auto-tool-choice --tool-call-parser hermes}"
ENGINE_EXTRA_ARGS="${ENGINE_EXTRA_ARGS:-}"
MEMCACHE_LIB_DIR="${MEMCACHE_LIB_DIR:-/usr/local/python3.12.13/lib/python3.12/site-packages/memcache_hybrid/lib}"
PYTHON_LIB_DIR="${PYTHON_LIB_DIR:-/usr/local/python3.12.13/lib}"

MOTOR_CONTAINER="${MOTOR_CONTAINER:-${KVC_CONTAINER_PREFIX}-motor}"
ENGINE_CONTAINER="${ENGINE_CONTAINER:-${KVC_CONTAINER_PREFIX}-engine-0}"
META_CONTAINER="${META_CONTAINER:-${KVC_CONTAINER_PREFIX}-memcache-meta}"

# Data
KVC_DATA_MODE="${KVC_DATA_MODE:-synthetic}"
SOURCE_DATA_DIR="${SOURCE_DATA_DIR:-}"
FAST_IDLE_OFFLOAD="${FAST_IDLE_OFFLOAD:-10}"
FAST_DELAY_MS="${FAST_DELAY_MS:-15000}"
FAST_S1_DURATION="${FAST_S1_DURATION:-120}"
FAST_S5_DURATION="${FAST_S5_DURATION:-180}"
STD_IDLE_OFFLOAD="${STD_IDLE_OFFLOAD:-30}"
STD_DELAY_MS="${STD_DELAY_MS:-45000}"
STD_S1_DURATION="${STD_S1_DURATION:-180}"
STD_S5_DURATION="${STD_S5_DURATION:-300}"

# Ascend / MemCache
ASCEND_DRIVER_PATH="${ASCEND_DRIVER_PATH:-/usr/local/Ascend/driver}"
ASCEND_INSTALL_INFO="${ASCEND_INSTALL_INFO:-/etc/ascend_install.info}"
ASCEND_DCMI="${ASCEND_DCMI:-/usr/local/dcmi}"
ASCEND_SET_ENV="${ASCEND_SET_ENV:-/usr/local/Ascend/ascend-toolkit/set_env.sh}"
MMC_WORLD_SIZE="${MMC_WORLD_SIZE:-1}"
MMC_DRAM_SIZE="${MMC_DRAM_SIZE:-64GB}"
MMC_MAX_DRAM_SIZE="${MMC_MAX_DRAM_SIZE:-256GB}"
NUM_CACHE_LEVELS="${NUM_CACHE_LEVELS:-2}"
KVC_SHM_KEY="${KVC_SHM_KEY:-kvcfunc}"
KVC_METRICS_SHM_NAME="${KVC_METRICS_SHM_NAME:-motor_kvc_metrics_kvcfunc}"

# Logs
COORD_LOG="${COORD_LOG:-$KVC_LOG_DIR/coordinator-$KVC_CONTAINER_PREFIX.log}"
ENGINE_LOG="${ENGINE_LOG:-$KVC_LOG_DIR/engine-$KVC_CONTAINER_PREFIX-$ENGINE_PORT.log}"

mkdir -p "$WORK_DIR" "$DATA_DIR" "$RESULTS_DIR" "$GENERATED_CONFIG_DIR" "$KVC_LOG_DIR" "$KVC_MMC_DIR"

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2
    exit 1
}

require_model() {
    [ -n "$MODEL_PATH" ] || die "MODEL_PATH is not configured; run qa_run.sh init and edit kvc_test.env"
    [ -e "$MODEL_PATH" ] || die "MODEL_PATH does not exist: $MODEL_PATH"
}

# Export the resolved configuration so Python helpers and child scripts see it.
export KIT_DIR BENCH_DIR BENCH_SOURCE_DIR KVC_TEST_ENV_FILE
export MODEL_PATH TOKENIZER_PATH MODEL_NAME MAX_MODEL_LEN CHAT_API TP_SIZE CARD CARDS GPU_MEM_UTIL ENGINE_START_TIMEOUT
export ENGINE_IMAGE MOTOR_IMAGE AIS_IMAGE MOTOR_INSTALL_MODE MOTOR_WHEEL MOTOR_SOURCE_DIR
export WORK_DIR DATA_DIR RESULTS_DIR GENERATED_CONFIG_DIR KVC_LOG_DIR KVC_MMC_DIR
export ENGINE_PORT MOTOR_INFER_PORT MOTOR_MGMT_PORT MOTOR_OBS_PORT
export MMC_META_PORT MMC_STORE_PORT MMC_METRICS_PORT MMC_LOOKUP_PORT
export HOST_IP DOCKER_BIN KVC_CONTAINER_PREFIX AIS_RUN_MODE AIS_PRE_CMD ENGINE_TOOL_ARGS ENGINE_EXTRA_ARGS MEMCACHE_LIB_DIR PYTHON_LIB_DIR
export MOTOR_CONTAINER ENGINE_CONTAINER META_CONTAINER
export KVC_DATA_MODE SOURCE_DATA_DIR FAST_IDLE_OFFLOAD FAST_DELAY_MS FAST_S1_DURATION FAST_S5_DURATION STD_IDLE_OFFLOAD STD_DELAY_MS STD_S1_DURATION STD_S5_DURATION
export ASCEND_DRIVER_PATH ASCEND_INSTALL_INFO ASCEND_DCMI ASCEND_SET_ENV
export MMC_WORLD_SIZE MMC_DRAM_SIZE MMC_MAX_DRAM_SIZE NUM_CACHE_LEVELS
export KVC_SHM_KEY KVC_METRICS_SHM_NAME COORD_LOG ENGINE_LOG
