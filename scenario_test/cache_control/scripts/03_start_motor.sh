#!/bin/bash
# 03 起网关 coordinator（arm -> 配置 + lifecycle env）
#   用法: 03_start_motor.sh <decl|off|life>
#   decl/off 用同一份字节相同的配置（差异只在请求侧发不发 header）；
#   life = lifecycle 开（MOTOR_CACHE_CONTROL_LIFECYCLE=1 + cfg.enabled=true），仅作对照。
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

NO_RESTART=0
if [ "${1:-}" = "--no-restart" ]; then NO_RESTART=1; shift; fi
ARM="${1:-}"
case "$ARM" in
  decl|off) ENVLC=0 ;;
  life)     ENVLC=1 ;;
  *) die "用法: $0 <decl|off|life>" ;;
esac

guard_container "$MOTOR_CONTAINER"
require_container "$MOTOR_CONTAINER"

if [ "$NO_RESTART" -eq 0 ]; then
  # 容器级重启：清掉共享内存/旧 coordinator 残留（臂隔离的一部分）
  say "重启网关容器 $MOTOR_CONTAINER（清 SHM/旧进程）"
  docker restart "$MOTOR_CONTAINER" >/dev/null
  for _ in $(seq 1 30); do sleep 1; docker exec "$MOTOR_CONTAINER" true 2>/dev/null && break; done
fi

SRC_CFG="$SCENARIO_ROOT/config/motor/$ARM.json"
[ -f "$SRC_CFG" ] || die "缺少配置: $SRC_CFG"

STAMP=$(date +%H%M%S)
RUN_CFG="$LOG_DIR/cfg-$ARM-$STAMP.json"
OUT="$LOG_DIR/motor-$ARM-lc$ENVLC-$STAMP.log"
cp "$SRC_CFG" "$RUN_CFG"

# 把模型路径 / 端口 / SHM 名注入本轮配置副本（保持原配置不动，便于审计）
python3 - "$RUN_CFG" "$MODEL_PATH" "$KVC_METRICS_SHM_NAME" \
  "$INFER_PORT" "$MGMT_PORT" "$OBS_PORT" "${WORKER_PORT:-19478}" <<'PY'
import json, sys
p, model, shm, ip, mp, op, wp = sys.argv[1:8]
d = json.load(open(p, encoding="utf-8"))
c = d.get("motor_coordinator_config") or d
c["context_budget_mode"] = "on"
c.setdefault("api_config", {})["coordinator_api_host"] = "127.0.0.1"
c["api_config"]["coordinator_api_infer_port"] = int(ip)
c["api_config"]["coordinator_api_mgmt_port"] = int(mp)
c["api_config"]["coordinator_obs_port"] = int(op)
c.setdefault("inference_workers_config", {})["worker_metaserver_base_port"] = int(wp)
sc = c.setdefault("scheduler_config", {})
kv = sc.setdefault("kv_conductor_config", {})
kv["model_path"] = model
c.setdefault("kvc_lifecycle_config", {})["metrics_shm_name"] = shm
json.dump(d, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PY

# 停掉旧 coordinator
PID=$(docker exec "$MOTOR_CONTAINER" sh -c 'pgrep -f motor.coordinator.main | head -1' 2>/dev/null || true)
if [ -n "$PID" ]; then
  say "停止旧 coordinator pid=$PID"
  docker exec "$MOTOR_CONTAINER" kill -TERM "$PID" >/dev/null 2>&1 || true
  sleep 6
fi

say "启动 coordinator: arm=$ARM cfg=$(basename "$RUN_CFG") envlc=$ENVLC"
docker exec -d \
  -e MOTOR_LOG_PATH="$LOG_DIR" \
  -e MOTOR_SESSION_AFFINITY=1 \
  -e MOTOR_SESSION_AFFINITY_PLACEMENT=balanced \
  -e MOTOR_SESSION_AFFINITY_ROUTE=0 \
  -e MOTOR_SESSION_AFFINITY_SHARED=1 \
  -e MOTOR_SESSION_AFFINITY_SHM_KEY="$SESSION_SHM_KEY" \
  -e USER_CONFIG_PATH="$RUN_CFG" \
  -e MOTOR_CACHE_CONTROL_MAX=65536 \
  -e MOTOR_CACHE_CONTROL_TTL_MAX_S=3600 \
  -e MOTOR_CACHE_CONTROL_SWEEP_INTERVAL_S=2 \
  -e MOTOR_CACHE_CONTROL_LIFECYCLE="$ENVLC" \
  "$MOTOR_CONTAINER" sh -c "cd /workspace && exec python3 -m motor.coordinator.main > $OUT 2>&1"

say "等待 liveness（:${MGMT_PORT}/liveness）..."
for _ in $(seq 1 40); do
  if curl -s --max-time 2 "http://127.0.0.1:${MGMT_PORT}/liveness" 2>/dev/null | grep -q ok; then break; fi
  sleep 2
done
curl -s --max-time 2 "http://127.0.0.1:${MGMT_PORT}/liveness" 2>/dev/null | grep -q ok \
  || die "coordinator 未就绪，见 $OUT"

# 生成并注册引擎实例（端口/模型名来自 env）
INST="$LOG_DIR/instances-$(date +%H%M%S).json"
SERVED_MODEL_NAME="$SERVED_MODEL_NAME" ENGINE_PORT="$ENGINE_PORT" \
  python3 - "$SCENARIO_ROOT/config/instances.json.tmpl" "$INST" <<'PY'
import os, sys
tmpl, out = sys.argv[1], sys.argv[2]
s = open(tmpl, encoding="utf-8").read()
s = s.replace("${SERVED_MODEL_NAME}", os.environ["SERVED_MODEL_NAME"])
s = s.replace("${ENGINE_PORT}", os.environ["ENGINE_PORT"])
open(out, "w", encoding="utf-8").write(s)
PY
curl -s --max-time 15 -X POST "http://127.0.0.1:${MGMT_PORT}/instances/refresh" \
  -H 'Content-Type: application/json' --data-binary "@$INST" >/dev/null || die "instances/refresh 失败"
sleep 2

READY=$(http "http://127.0.0.1:${MGMT_PORT}/liveness" 3)
say "ready=$READY  (cfg=$RUN_CFG, log=$OUT, instances=$INST)"
[ "$READY" = "200" ] || die "liveness 非 200"
