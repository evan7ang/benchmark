#!/usr/bin/env bash
# Start an isolated single-card TP1 stack for KVC functional testing.
#
# It uses its own containers (ttft52113-*), ports (18200/19225-19227/15200-18281),
# runtime directory (/home/t00887700/kv-hint/.build/ttft52113), and shared-memory
# keys, so it never disturbs the performance-test stack (agentx-*).
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
ENGINE_MAX_LEN="32768"
ENGINE_GMU="0.85"

usage() {
    cat <<EOF
Usage: $0 [options]
  --kvc on|off             KVC lifecycle enabled (default: on)
  --card N                 NPU card for the TP1 engine (default: $CARD)
  --port N                 engine port (default: $ENGINE_PORT)
  --idle-offload S         idle_offload_s (default: 30)
  --idle-evict S           idle_evict_s (default: 0)
  --high-water F           offload_kv_usage_high_water (default: 0)
  --prefetch on|off        prefetch_enabled (default: on)
  --max-model-len N        engine max_model_len (default: 32768)
  --gmu F                  engine gpu_memory_utilization (default: 0.85)
  --no-engine              reuse the existing engine container
  --no-meta                reuse the existing memcache meta container
  --engine-only            start only memcache + engine
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --kvc) KVC="$2"; shift 2 ;;
        --card) CARD="$2"; shift 2 ;;
        --port) ENGINE_PORT="$2"; shift 2 ;;
        --idle-offload) IDLE_OFFLOAD="$2"; shift 2 ;;
        --idle-evict) IDLE_EVICT="$2"; shift 2 ;;
        --high-water) HIGH_WATER="$2"; shift 2 ;;
        --prefetch) PREFETCH="$2"; shift 2 ;;
        --max-model-len) ENGINE_MAX_LEN="$2"; shift 2 ;;
        --gmu) ENGINE_GMU="$2"; shift 2 ;;
        --no-engine) START_ENGINE=0; shift ;;
        --no-meta) START_META=0; shift ;;
        --engine-only) ENGINE_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

ENGINE_LOG="$KVC_LOG_DIR/engine-ttft52113-$ENGINE_PORT.log"
ENGINE_LOG_LEVEL="${ENGINE_LOG_LEVEL:-INFO}"
export ENGINE_LOG

if [ "$START_ENGINE" = "0" ] && [ "$ENGINE_ONLY" = "0" ]; then
    curl -sf --max-time 3 "http://127.0.0.1:${ENGINE_PORT}/v1/models" >/dev/null 2>&1 \
      || die "--no-engine was given but no engine is healthy on :$ENGINE_PORT"
fi

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
ock.mmc.local_service.world_size = 1
ock.mmc.local_service.protocol = host_shm
ock.mmc.local_service.dram.size = 64GB
ock.mmc.local_service.max.dram.size = 256GB
CONF
    for _ in 1 2 3; do docker rm -f "$META_CONTAINER" >/dev/null 2>&1 && break; sleep 2; done
    docker run -d --name "$META_CONTAINER" --privileged --network host --ipc private --shm-size 8g \
      --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
      -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
      -v /etc/ascend_install.info:/etc/ascend_install.info \
      -v /usr/local/dcmi:/usr/local/dcmi \
      -v "$KVC_MMC_DIR":/memcache \
      -e ASCEND_RUNTIME_OPTIONS=NODRV -e PYTHONHASHSEED=0 \
      -e MMC_META_CONFIG_PATH=/memcache/mmc-meta.conf \
      -e LD_LIBRARY_PATH=/usr/local/python3.12.13/lib/python3.12/site-packages/memcache_hybrid/lib:/usr/local/python3.12.13/lib \
      --entrypoint bash "$ENGINE_IMG" \
      -lc 'python3 -c "from memcache_hybrid import MetaService; MetaService.main()"' >/dev/null
    for _ in $(seq 1 60); do
        if curl -sf --max-time 1 "http://127.0.0.1:${MMC_METRICS_PORT}/metrics" >/dev/null 2>&1; then
            log "memcache meta ready (metrics :$MMC_METRICS_PORT)"
            return 0
        fi
        sleep 1
    done
    docker logs --tail 40 "$META_CONTAINER" >&2 || true
    die "memcache meta failed to become ready"
}

start_engine() {
    log "starting isolated TP1 engine ($ENGINE_CONTAINER) on card $CARD port $ENGINE_PORT"
    for _ in 1 2 3; do docker rm -f "$ENGINE_CONTAINER" >/dev/null 2>&1 && break; sleep 2; done
    docker run -d --name "$ENGINE_CONTAINER" --privileged --network host --ipc private --shm-size 64g \
      --entrypoint bash \
      --device "/dev/davinci${CARD}" \
      --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
      -e ASCEND_RT_VISIBLE_DEVICES="$CARD" -e ASCEND_RUNTIME_OPTIONS=NODRV \
      -e VLLM_ASCEND_ENABLE_AGENT_HINT=1 -e NUM_CACHE_LEVELS=2 -e PYTHONHASHSEED=0 \
      -e MMC_LOCAL_CONFIG_PATH=/memcache/mmc-local.conf \
      -e LD_LIBRARY_PATH=/usr/local/python3.12.13/lib/python3.12/site-packages/memcache_hybrid/lib:/usr/local/python3.12.13/lib \
      -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
      -v /etc/ascend_install.info:/etc/ascend_install.info \
      -v /usr/local/dcmi:/usr/local/dcmi \
      -v "$MODEL_PATH":"$MODEL_PATH" \
      -v "$KVC_MMC_DIR":/memcache \
      -v "$KVC_LOG_DIR":"$KVC_LOG_DIR" \
      "$ENGINE_IMG" -c 'sleep infinity' >/dev/null
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

    docker exec -d "$ENGINE_CONTAINER" bash -lc "
      source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
      export ASCEND_RT_VISIBLE_DEVICES=$CARD ASCEND_RUNTIME_OPTIONS=NODRV
      export PYTHONHASHSEED=0 NUM_CACHE_LEVELS=2 VLLM_ASCEND_ENABLE_AGENT_HINT=1
      export MMC_LOCAL_CONFIG_PATH=/memcache/mmc-local.conf
      export LD_LIBRARY_PATH=/usr/local/python3.12.13/lib/python3.12/site-packages/memcache_hybrid/lib:/usr/local/python3.12.13/lib:\$LD_LIBRARY_PATH
      export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 VLLM_SERVER_DEV_MODE=1
      export VLLM_LOGGING_LEVEL=$ENGINE_LOG_LEVEL
      cd /vllm-workspace/vllm 2>/dev/null || true
      exec > '$ENGINE_LOG' 2>&1
      vllm serve '$MODEL_PATH' --served-model-name kvh --port '$ENGINE_PORT' \
        --tensor-parallel-size 1 --max-model-len '$ENGINE_MAX_LEN' --gpu-memory-utilization '$ENGINE_GMU' \
        --enforce-eager --enable-prefix-caching --enable-auto-tool-choice --tool-call-parser hermes \
        --kv-transfer-config '$kv_config'
    "
    for _ in $(seq 1 600); do
        if curl -sf --max-time 2 "http://127.0.0.1:${ENGINE_PORT}/v1/models" >/dev/null 2>&1; then
            log "engine ready: http://127.0.0.1:${ENGINE_PORT}"
            return 0
        fi
        sleep 2
    done
    tail -80 "$ENGINE_LOG" >&2 || true
    die "engine failed to become ready"
}

start_motor() {
    local cfg="$KVC_LOG_DIR/coordinator-ttft52113.json"
    local kvc_enabled="true"
    local prefetch_enabled="true"
    [ "$KVC" = "on" ] || kvc_enabled="false"
    [ "$PREFETCH" = "on" ] || prefetch_enabled="false"

    log "starting isolated Motor ($MOTOR_CONTAINER) kvc=$KVC idle_offload=$IDLE_OFFLOAD high_water=$HIGH_WATER"
    python3 - "$cfg" "$KVC" "$IDLE_OFFLOAD" "$IDLE_EVICT" "$HIGH_WATER" "$PREFETCH" \
        "$MOTOR_INFER_PORT" "$MOTOR_MGMT_PORT" "$MOTOR_OBS_PORT" <<'PY'
import json, sys
cfg_path, kvc, idle_offload, idle_evict, high_water, prefetch, infer_port, mgmt_port, obs_port = sys.argv[1:]
cfg = {"motor_coordinator_config": {
    "aigw": {"id": "kvh", "p_max_seqlen": 32768, "d_max_seqlen": 32768},
    "api_config": {
        "coordinator_api_host": "127.0.0.1",
        "coordinator_api_infer_port": int(infer_port),
        "coordinator_api_mgmt_port": int(mgmt_port),
        "coordinator_obs_port": int(obs_port),
    },
    "inference_workers_config": {"num_workers": 1, "worker_metaserver_base_port": 19229},
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
        "metrics_shm_name": "motor_kvc_metrics_ttft52113",
    },
}}
with open(cfg_path, "w") as f:
    json.dump(cfg, f)
PY

    for _ in 1 2 3; do docker rm -f "$MOTOR_CONTAINER" >/dev/null 2>&1 && break; sleep 2; done
    docker run -d --name "$MOTOR_CONTAINER" --network host --ipc host \
      -v "$KVHINT:$KVHINT" -v "$MODEL_PATH:$MODEL_PATH" -v "$KVC_LOG_DIR:$KVC_LOG_DIR" \
      "$MOTOR_IMG" bash -c 'sleep infinity' >/dev/null
    sleep 2

    if [ -f "$MOTOR_WHEEL" ]; then
        log "installing Motor wheel: $MOTOR_WHEEL"
        docker exec "$MOTOR_CONTAINER" bash -lc \
          "pip install --force-reinstall --no-deps '$MOTOR_WHEEL'" \
          >> "$KVC_LOG_DIR/motor-pip.log" 2>&1 || die "Motor wheel install failed"
    else
        log "WARN Motor wheel missing; using the wheel baked into the image"
    fi

    docker exec -d \
      -e MOTOR_SESSION_AFFINITY=1 \
      -e MOTOR_SESSION_AFFINITY_SHARED=1 \
      -e MOTOR_SESSION_AFFINITY_PLACEMENT=balanced \
      -e MOTOR_SESSION_AFFINITY_ROUTE=0 \
      -e MOTOR_SESSION_AFFINITY_SHM_KEY="$AFFINITY_SHM_KEY" \
      "$MOTOR_CONTAINER" bash -lc "
      export USER_CONFIG_PATH='$cfg'
      export MOTOR_LOG_PATH='$KVC_LOG_DIR'
      exec > '$COORD_LOG' 2>&1
      exec python3 -m motor.coordinator.main
    "

    for _ in $(seq 1 120); do
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${MOTOR_MGMT_PORT}/readiness" || true)
        [ "$code" = "200" ] && break
        sleep 2
    done

    local inst="$KVC_LOG_DIR/instances-ttft52113.json"
    python3 - "$inst" "$ENGINE_PORT" <<'PY'
import json, sys
path, port = sys.argv[1], sys.argv[2]
ep = {"id": 0, "ip": "127.0.0.1", "business_port": str(port), "status": "normal"}
inst = [{
    "job_name": "ttft52113-union-1", "model_name": "kvh", "engine_type": "vllm",
    "dispatch_capabilities": ["concurrent_engine_sync"], "id": 1, "role": "union",
    "status": "active", "parallel_config": {"dp_size": 1}, "enable_multi_endpoints": True,
    "endpoints": {"127.0.0.1": {"0": ep}},
}]
with open(path, "w") as f:
    json.dump({"event": "set", "instances": inst}, f)
PY
    curl -s -o /dev/null -X POST "http://127.0.0.1:${MOTOR_MGMT_PORT}/instances/refresh" \
      -H 'Content-Type: application/json' --data-binary @"$inst"
    sleep 3
    local count
    count=$(curl -s "http://127.0.0.1:${MOTOR_MGMT_PORT}/instances" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count"))' 2>/dev/null || echo "?")
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
