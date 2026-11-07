#!/usr/bin/env bash
# Shared paths and defaults for the TTFT Issue #52113 reproduction kit.
#
# This kit is isolated from both the performance harness (agentx-*) and the
# KVC functional kit (kvcfunc-*): own containers, ports, card and runtime dir.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH_DIR="$(cd "$KIT_DIR/../../.." && pwd)"
KVHINT="${KVHINT:-/home/t00887700/kv-hint}"
AGENTX="${AGENTX:-$KVHINT/.build/agentx}"
BENCH_DATA="${BENCH_DATA:-/home/t00887700/benchmark-data/agent_replay_v2}"
MODEL_PATH="${MODEL_PATH:-/home/RAG_data/models/Qwen3-8B}"

MOTOR_IMG="${MOTOR_IMG:-mindie-motor-vllm:e2e3b}"
ENGINE_IMG="${ENGINE_IMG:-agenthint-vllm:0.1.0-p3memcache}"
AIS_IMG="${AIS_IMG:-ais-runner:ais-1}"
MOTOR_WHEEL="${MOTOR_WHEEL:-$AGENTX/motor-wheel-kvc-func/motor-3.1.0-py3-none-linux_aarch64.whl}"

# Dedicated runtime directory: logs, memcache conf, evidence.
KVC_RUNTIME_DIR="${KVC_RUNTIME_DIR:-$KVHINT/.build/ttft52113}"
KVC_LOG_DIR="${KVC_LOG_DIR:-$KVC_RUNTIME_DIR/logs}"
KVC_MMC_DIR="${KVC_MMC_DIR:-$KVC_RUNTIME_DIR/memcache}"

# Dedicated ports (nothing here overlaps agentx-* / kvcfunc-*).
ENGINE_PORT="${ENGINE_PORT:-18200}"
MOTOR_INFER_PORT="${MOTOR_INFER_PORT:-19225}"
MOTOR_MGMT_PORT="${MOTOR_MGMT_PORT:-19226}"
MOTOR_OBS_PORT="${MOTOR_OBS_PORT:-19227}"
MMC_META_PORT="${MMC_META_PORT:-15200}"
MMC_STORE_PORT="${MMC_STORE_PORT:-16200}"
MMC_METRICS_PORT="${MMC_METRICS_PORT:-18281}"
MMC_LOOKUP_PORT="${MMC_LOOKUP_PORT:-19200}"

MOTOR_CONTAINER="${MOTOR_CONTAINER:-ttft52113-motor}"
ENGINE_CONTAINER="${ENGINE_CONTAINER:-ttft52113-engine-0}"
META_CONTAINER="${META_CONTAINER:-ttft52113-memcache-meta}"
AFFINITY_SHM_KEY="${AFFINITY_SHM_KEY:-ttft52113}"

# Single free card. Override with CARD=<n>.
CARD="${CARD:-2}"

RESULTS_DIR="${RESULTS_DIR:-$KIT_DIR/results}"
COORD_LOG="${COORD_LOG:-$KVC_LOG_DIR/coordinator-ttft52113.log}"
ENGINE_LOG="${ENGINE_LOG:-$KVC_LOG_DIR/engine-ttft52113-$ENGINE_PORT.log}"

mkdir -p "$RESULTS_DIR" "$KVC_LOG_DIR" "$KVC_MMC_DIR"

log() {
    printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2
    exit 1
}
