#!/usr/bin/env bash
# Start an isolated single-card/TP stack for KVC functional testing.
#
# Every path, image, port, card, and model name comes from kvc_test.env.
# Containers use the KVC_CONTAINER_PREFIX namespace so they never disturb a
# performance-test stack.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

KVC="on"
IDLE_OFFLOAD="30"
IDLE_EVICT="0"
HIGH_WATER="0"
PREFETCH="on"
START_ENGINE=1
START_META=1
ENGINE_ONLY=0
ENGINE_EXTRA_ARGS="${ENGINE_EXTRA_ARGS:-}"
TOOL_ARGS="${ENGINE_TOOL_ARGS-"--enable-auto-tool-choice --tool-call-parser hermes"}"

usage() {
    cat <<EOF
Usage: $0 [options]
  --kvc on|off             KVC lifecycle enabled (default: on)
  --card N                 NPU card (default: $CARD)
  --port N                 engine port (default: $ENGINE_PORT)
  --idle-offload S         idle_offload_s (default: 30)
  --idle-evict S           idle_evict_s (default: 0)
  --high-water F           offload_kv_usage_high_water (default: 0)
  --prefetch on|off        prefetch_enabled (default: on)
  --no-engine              reuse the existing engine container
  --no-meta                reuse the existing memcache meta container
  --engine-only            start only memcache + engine
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --kvc) KVC="$2"; shift 2 ;;
        --card) CARD="$2"; CARDS="$2"; shift 2 ;;
        --port) ENGINE_PORT="$2"; shift 2 ;;
        --idle-offload) IDLE_OFFLOAD="$2"; shift 2 ;;
        --idle-evict) IDLE_EVICT="$2"; shift 2 ;;
        --high-water) HIGH_WATER="$2"; shift 2 ;;
        --prefetch) PREFETCH="$2"; shift 2 ;;
        --no-engine) START_ENGINE=0; shift ;;
        --no-meta) START_META=0; shift ;;
        --engine-only) ENGINE_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

require_model
ENGINE_LOG="$KVC_LOG_DIR/engine-$KVC_CONTAINER_PREFIX-$ENGINE_PORT.log"
export ENGINE_LOG
CARDS="${CARDS:-$CARD}"
TP_SIZE="${TP_SIZE:-1}"
MOTOR_WORKER_META_PORT="${MOTOR_WORKER_META_PORT:-$((MOTOR_MGMT_PORT + 3))}"

if [ "$START_ENGINE" = "0" ] && [ "$ENGINE_ONLY" = "0" ]; then
    curl -sf --max-time 3 "http://${HOST_IP}:${ENGINE_PORT}/v1/models" >/dev/null 2>&1 \
      || die "--no-engine was given but no engine is healthy on ${HOST_IP}:$ENGINE_PORT"
fi

remove_with_retry() {
    local name="$1"
    for _ in 1 2 3; do
        "$DOCKER_BIN" rm -f "$name" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 0
}

mount_if_exists() {
    local host_path="$1"
    if [ -e "$host_path" ]; then
        printf '%s' "-v $host_path:$host_path"
    fi
}

device_args() {
    local args=()
    IFS=',' read -r -a cards <<< "$CARDS"
    for card in "${cards[@]}"; do
        args+=(--device "/dev/davinci${card}")
    done
    args+=(--device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc)
    printf '%s\n' "${args[@]}"
}

start_meta() {
    log "starting isolated memcache meta ($META_CONTAINER)"
    cat > "$KVC_MMC_DIR/mmc-meta.conf" <<CONF
ock.mmc.meta_service_url = tcp://127.0.0.1:${MMC_META_PORT}
ock.mmc.meta_service.config_store_url = tcp://127.0.0.1:${MMC_STORE_PORT}
ock.mmc.meta_service.metrics_url = http://127.0.0.1:${MMC_METRICS_PORT}
ock.mmc.log_level = info
CONF
    cat > "$KVC_MMC_DIR/mmc-local.conf" <<CONF
ock.mmc.meta_service_url = tcp://127.0.0.1:${MMC_META_PORT}
ock.mmc.local_service.config_store_url = tcp://127.0.0.1:${MMC_STORE_PORT}
ock.mmc.log_level = info
ock.mmc.local_service.world_size = ${MMC_WORLD_SIZE}
ock.mmc.local_service.protocol = host_shm
ock.mmc.local_service.dram.size = ${MMC_DRAM_SIZE}
ock.mmc.local_service.max.dram.size = ${MMC_MAX_DRAM_SIZE}
CONF
    remove_with_retry "$META_CONTAINER"
    local driver_mount install_mount dcmi_mount
    driver_mount=$(mount_if_exists "$ASCEND_DRIVER_PATH")
    install_mount=$(mount_if_exists "$ASCEND_INSTALL_INFO")
    dcmi_mount=$(mount_if_exists "$ASCEND_DCMI")
    # shellcheck disable=SC2086
    "$DOCKER_BIN" run -d --name "$META_CONTAINER" --privileged --network host --ipc private --shm-size 8g \
      --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
      $driver_mount $install_mount $dcmi_mount \
      -v "$KVC_MMC_DIR":/memcache \
      -e ASCEND_RUNTIME_OPTIONS=NODRV -e PYTHONHASHSEED=0 \
      -e MMC_META_CONFIG_PATH=/memcache/mmc-meta.conf \
      -e LD_LIBRARY_PATH="$MEMCACHE_LIB_DIR:$PYTHON_LIB_DIR" \
      --entrypoint bash "$ENGINE_IMAGE" \
      -lc 'python3 -c "from memcache_hybrid import MetaService; MetaService.main()"' >/dev/null
    for _ in $(seq 1 60); do
        if curl -sf --max-time 1 "http://127.0.0.1:${MMC_METRICS_PORT}/metrics" >/dev/null 2>&1; then
            log "memcache meta ready (metrics :$MMC_METRICS_PORT)"
            return 0
        fi
        sleep 1
    done
    "$DOCKER_BIN" logs --tail 40 "$META_CONTAINER" >&2 || true
    die "memcache meta failed to become ready"
}

start_engine() {
    log "starting isolated TP$TP_SIZE engine ($ENGINE_CONTAINER) cards=$CARDS port=$ENGINE_PORT"
    remove_with_retry "$ENGINE_CONTAINER"
    local dev_args
    mapfile -t dev_args < <(device_args)
    local driver_mount install_mount dcmi_mount
    driver_mount=$(mount_if_exists "$ASCEND_DRIVER_PATH")
    install_mount=$(mount_if_exists "$ASCEND_INSTALL_INFO")
    dcmi_mount=$(mount_if_exists "$ASCEND_DCMI")
    # shellcheck disable=SC2086
    "$DOCKER_BIN" run -d --name "$ENGINE_CONTAINER" --privileged --network host --ipc private --shm-size 64g \
      --entrypoint bash \
      "${dev_args[@]}" \
      -e ASCEND_RT_VISIBLE_DEVICES="$CARDS" -e ASCEND_RUNTIME_OPTIONS=NODRV \
      -e VLLM_ASCEND_ENABLE_AGENT_HINT=1 -e NUM_CACHE_LEVELS="$NUM_CACHE_LEVELS" -e PYTHONHASHSEED=0 \
      -e MMC_LOCAL_CONFIG_PATH=/memcache/mmc-local.conf \
      -e LD_LIBRARY_PATH="$MEMCACHE_LIB_DIR:$PYTHON_LIB_DIR" \
      $driver_mount $install_mount $dcmi_mount \
      -v "$MODEL_PATH:$MODEL_PATH" \
      -v "$KVC_MMC_DIR":/memcache \
      -v "$KVC_LOG_DIR":"$KVC_LOG_DIR" \
      "$ENGINE_IMAGE" -c 'sleep infinity' >/dev/null
    sleep 2

    local kv_config
    kv_config=$(python3 - "$MMC_LOOKUP_PORT" <<'PY'
import json, sys
print(json.dumps({
    "kv_connector": "AscendStoreConnector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {
        "backend": "memcache",
        "lookup_rpc_port": sys.argv[1],
        "use_layerwise": False,
    },
}, separators=(",", ":")))
PY
)

    : > "$ENGINE_LOG"
    "$DOCKER_BIN" exec -d "$ENGINE_CONTAINER" bash -lc "
      if [ -f '$ASCEND_SET_ENV' ]; then source '$ASCEND_SET_ENV' 2>/dev/null || true; fi
      export ASCEND_RT_VISIBLE_DEVICES=$CARDS ASCEND_RUNTIME_OPTIONS=NODRV
      export PYTHONHASHSEED=0 NUM_CACHE_LEVELS=$NUM_CACHE_LEVELS VLLM_ASCEND_ENABLE_AGENT_HINT=1
      export MMC_LOCAL_CONFIG_PATH=/memcache/mmc-local.conf
      export LD_LIBRARY_PATH=$MEMCACHE_LIB_DIR:$PYTHON_LIB_DIR:\$LD_LIBRARY_PATH
      export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 VLLM_SERVER_DEV_MODE=1
      cd /vllm-workspace/vllm 2>/dev/null || true
      exec > '$ENGINE_LOG' 2>&1
      vllm serve '$MODEL_PATH' --served-model-name '$MODEL_NAME' --host 0.0.0.0 --port '$ENGINE_PORT' \
        --tensor-parallel-size '$TP_SIZE' --max-model-len '$MAX_MODEL_LEN' --gpu-memory-utilization '$GPU_MEM_UTIL' \
        --enforce-eager --enable-prefix-caching $TOOL_ARGS $ENGINE_EXTRA_ARGS \
        --kv-transfer-config '$kv_config'
    "
    local tries=$(( ENGINE_START_TIMEOUT / 2 ))
    [ "$tries" -lt 10 ] && tries=10
    local i=0
    while [ "$i" -lt "$tries" ]; do
        i=$((i + 1))
        if curl -sf --max-time 2 "http://${HOST_IP}:${ENGINE_PORT}/v1/models" >/dev/null 2>&1; then
            log "engine ready: http://${HOST_IP}:${ENGINE_PORT}"
            return 0
        fi
        if [ "$i" -gt 5 ] && grep -qE 'Engine core initialization failed|ERR99999|ValueError: Free memory' "$ENGINE_LOG" 2>/dev/null; then
            tail -40 "$ENGINE_LOG" >&2 || true
            die "engine failed to start quickly; see $ENGINE_LOG (check CARD free memory / GPU_MEM_UTIL)"
        fi
        sleep 2
    done
    tail -80 "$ENGINE_LOG" >&2 || true
    die "engine failed to become ready within ${ENGINE_START_TIMEOUT}s"
}

start_motor() {
    local cfg="$KVC_LOG_DIR/coordinator-$KVC_CONTAINER_PREFIX.json"
    log "starting isolated Motor ($MOTOR_CONTAINER) kvc=$KVC mode=$MOTOR_INSTALL_MODE"
    python3 - "$cfg" "$KVC" "$IDLE_OFFLOAD" "$IDLE_EVICT" "$HIGH_WATER" "$PREFETCH" \
        "$MOTOR_INFER_PORT" "$MOTOR_MGMT_PORT" "$MOTOR_OBS_PORT" "$MOTOR_WORKER_META_PORT" \
        "$MODEL_NAME" "$MAX_MODEL_LEN" "$KVC_METRICS_SHM_NAME" <<'PY'
import json, sys
(cfg_path, kvc, idle_offload, idle_evict, high_water, prefetch, infer_port,
 mgmt_port, obs_port, worker_meta_port, model_name, max_len, metrics_shm) = sys.argv[1:]
cfg = {"motor_coordinator_config": {
    "aigw": {"id": model_name, "p_max_seqlen": int(max_len), "d_max_seqlen": int(max_len)},
    "api_config": {
        "coordinator_api_host": "0.0.0.0",
        "coordinator_api_infer_port": int(infer_port),
        "coordinator_api_mgmt_port": int(mgmt_port),
        "coordinator_obs_port": int(obs_port),
    },
    "inference_workers_config": {"num_workers": 1, "worker_metaserver_base_port": int(worker_meta_port)},
    "scheduler_config": {
        "prefill_scheduler_type": "load_balance",
        "decode_scheduler_type": "load_balance",
        "affinity_load_gate_ratio": 0.0,
    },
    "rate_limit_config": {"enable_rate_limit": False},
    "exception_config": {"first_token_timeout": 1800},
    "agent_status_config": {"enabled": True, "central_lifecycle": True},
    "kvc_lifecycle_config": {
        "enabled": kvc == "on",
        "pin_ttl_s": 3600,
        "idle_offload_s": float(idle_offload),
        "idle_evict_s": float(idle_evict),
        "offload_kv_usage_high_water": float(high_water),
        "prefetch_enabled": prefetch == "on",
        "metrics_shm_name": metrics_shm,
    },
}}
with open(cfg_path, "w") as handle:
    json.dump(cfg, handle)
PY

    remove_with_retry "$MOTOR_CONTAINER"
    local total_mem
    total_mem=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}' || echo 0)
    local shm_size="8g"
    [ "${total_mem:-0}" -ge 128 ] && shm_size="64g"
    local wheel_mount=""
    if [ "$MOTOR_INSTALL_MODE" = "wheel" ] && [ -n "$MOTOR_WHEEL" ]; then
        wheel_mount="-v $(dirname "$MOTOR_WHEEL"):$(dirname "$MOTOR_WHEEL"):ro"
    elif [ "$MOTOR_INSTALL_MODE" = "source" ] && [ -n "$MOTOR_SOURCE_DIR" ]; then
        wheel_mount="-v $MOTOR_SOURCE_DIR:$MOTOR_SOURCE_DIR:ro"
    fi
    # shellcheck disable=SC2086
    "$DOCKER_BIN" run -d --name "$MOTOR_CONTAINER" --network host --ipc host --shm-size "$shm_size" \
      -v "$BENCH_DIR:$BENCH_DIR" \
      -v "$MODEL_PATH:$MODEL_PATH" \
      -v "$KVC_LOG_DIR:$KVC_LOG_DIR" \
      $wheel_mount \
      "$MOTOR_IMAGE" bash -c 'sleep infinity' >/dev/null
    sleep 2

    case "$MOTOR_INSTALL_MODE" in
        image)
            "$DOCKER_BIN" exec "$MOTOR_CONTAINER" bash -lc 'python3 -c "import motor"' \
              || die "Motor image does not contain the motor package: $MOTOR_IMAGE"
            ;;
        wheel)
            [ -n "$MOTOR_WHEEL" ] && [ -f "$MOTOR_WHEEL" ] \
              || die "MOTOR_INSTALL_MODE=wheel but MOTOR_WHEEL is missing: $MOTOR_WHEEL"
            log "installing Motor wheel: $MOTOR_WHEEL"
            "$DOCKER_BIN" exec "$MOTOR_CONTAINER" bash -lc \
              "pip install --force-reinstall --no-deps '$MOTOR_WHEEL'" \
              >> "$KVC_LOG_DIR/motor-pip.log" 2>&1 || die "Motor wheel install failed"
            ;;
        source)
            local wheel
            wheel=$(ls -t "$MOTOR_SOURCE_DIR"/dist/motor-*.whl 2>/dev/null | head -1 || true)
            [ -n "$wheel" ] || die "MOTOR_INSTALL_MODE=source but no wheel under $MOTOR_SOURCE_DIR/dist; run qa_run.sh prepare-motor"
            log "installing Motor wheel from source build: $wheel"
            "$DOCKER_BIN" exec "$MOTOR_CONTAINER" bash -lc \
              "pip install --force-reinstall --no-deps '$wheel'" \
              >> "$KVC_LOG_DIR/motor-pip.log" 2>&1 || die "Motor wheel install failed"
            ;;
        *)
            die "unknown MOTOR_INSTALL_MODE=$MOTOR_INSTALL_MODE (image|wheel|source)"
            ;;
    esac

    "$DOCKER_BIN" exec -d \
      -e MOTOR_SESSION_AFFINITY=1 \
      -e MOTOR_SESSION_AFFINITY_SHARED=1 \
      -e MOTOR_SESSION_AFFINITY_PLACEMENT=balanced \
      -e MOTOR_SESSION_AFFINITY_ROUTE=0 \
      -e MOTOR_SESSION_AFFINITY_SHM_KEY="$KVC_SHM_KEY" \
      "$MOTOR_CONTAINER" bash -lc "
      export USER_CONFIG_PATH='$cfg'
      export MOTOR_LOG_PATH='$KVC_LOG_DIR'
      exec > '$COORD_LOG' 2>&1
      exec python3 -m motor.coordinator.main
    "

    for _ in $(seq 1 120); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST_IP}:${MOTOR_MGMT_PORT}/readiness" || true)
        [ "$code" = "200" ] && break
        sleep 2
    done

    local inst="$KVC_LOG_DIR/instances-$KVC_CONTAINER_PREFIX.json"
    python3 - "$inst" "$ENGINE_PORT" "$MODEL_NAME" "$HOST_IP" "$KVC_CONTAINER_PREFIX" <<'PY'
import json, sys
path, port, model_name, host_ip, prefix = sys.argv[1:]
ep = {"id": 0, "ip": host_ip, "business_port": str(port), "status": "normal"}
inst = [{
    "job_name": f"{prefix}-union-1", "model_name": model_name, "engine_type": "vllm",
    "dispatch_capabilities": ["concurrent_engine_sync"], "id": 1, "role": "union",
    "status": "active", "parallel_config": {"dp_size": 1}, "enable_multi_endpoints": True,
    "endpoints": {host_ip: {"0": ep}},
}]
with open(path, "w") as handle:
    json.dump({"event": "set", "instances": inst}, handle)
PY
    curl -s -o /dev/null -X POST "http://${HOST_IP}:${MOTOR_MGMT_PORT}/instances/refresh" \
      -H 'Content-Type: application/json' --data-binary @"$inst"
    sleep 3
    local count
    count=$(curl -s "http://${HOST_IP}:${MOTOR_MGMT_PORT}/instances" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count"))' 2>/dev/null || echo "?")
    log "Motor ready: infer=:$MOTOR_INFER_PORT mgmt=:$MOTOR_MGMT_PORT obs=:$MOTOR_OBS_PORT instances=$count"
}

if [ "$START_META" = "1" ]; then
    start_meta
fi
if [ "$START_ENGINE" = "1" ]; then
    start_engine
fi
if [ "$ENGINE_ONLY" = "1" ]; then
    log "engine-only stack ready"
    exit 0
fi

start_motor
log "isolated KVC functional stack is ready"
