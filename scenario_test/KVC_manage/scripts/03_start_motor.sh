#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ARM="${1:?usage: 03_start_motor.sh off|on}"
CFG="$(arm_cfg "$ARM")"
RUN_CFG="$MOTOR_LOG_DIR/kvcmanage-$(basename "$CFG")"
cp "$CFG" "$RUN_CFG"
CFG="$RUN_CFG"
LC="$(arm_lc "$ARM")"
TAG="$(basename "$CFG" .json)-lc$LC"
LOG="$MOTOR_LOG_DIR/motor-kvcmanage-$TAG-$(date +%m%d-%H%M%S).log"
say "restart motor $MOTOR_CONTAINER arm=$ARM cfg=$CFG lc=$LC"

OLD=$(docker exec "$MOTOR_CONTAINER" sh -c 'ps -eo pid,args | grep "motor.coordinator.main" | grep -v grep | awk "{print \$1}"' 2>/dev/null | head -1 || true)
[ -n "${OLD:-}" ] && docker exec "$MOTOR_CONTAINER" kill -TERM "$OLD" 2>/dev/null || true
sleep 4
docker start "$MOTOR_CONTAINER" >/dev/null 2>&1 || true
docker exec -d \
  -e MOTOR_LOG_PATH="$MOTOR_LOG_DIR" \
  -e MOTOR_SESSION_AFFINITY=1 \
  -e MOTOR_SESSION_AFFINITY_PLACEMENT=balanced \
  -e MOTOR_SESSION_AFFINITY_ROUTE=0 \
  -e MOTOR_SESSION_AFFINITY_SHARED=1 \
  -e MOTOR_SESSION_AFFINITY_SHM_KEY=ttft52113laneA \
  -e USER_CONFIG_PATH="$CFG" \
  -e MOTOR_CACHE_CONTROL_MAX=65536 \
  -e MOTOR_CACHE_CONTROL_TTL_MAX_S=3600 \
  -e MOTOR_CACHE_CONTROL_SWEEP_INTERVAL_S=2 \
  -e MOTOR_CACHE_CONTROL_LIFECYCLE="$LC" \
  "$MOTOR_CONTAINER" sh -c "cd /workspace && exec python3 -m motor.coordinator.main > '$LOG' 2>&1"

for i in $(seq 1 40); do
  sleep 2
  curl -s --max-time 2 "http://127.0.0.1:$MOTOR_MGMT_PORT/liveness" 2>/dev/null | grep -q ok && break
done
INST="$MOTOR_LOG_DIR/instances-ttft52113.json"
if [ -f "$INST" ]; then
  curl -s --max-time 15 -X POST "http://127.0.0.1:$MOTOR_MGMT_PORT/instances/refresh" \
    -H 'Content-Type: application/json' --data-binary "@$INST" >/dev/null 2>&1 || true
fi
sleep 2
say "motor ready=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$MOTOR_MGMT_PORT/liveness")"

