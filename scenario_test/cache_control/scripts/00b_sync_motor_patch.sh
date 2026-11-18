#!/bin/bash
# 00b 把「补丁版 motor 包」同步进电机容器（stock 镜像缺两样东西：
#   1) agent_hint.py 的 resume 白名单；2) obs 口 /internal/agent-status 路由）
# 补丁包还需要 grpcio>=1.84.1（生成代码的版本校验）。
#
#   用法: MOTOR_PATCH_SRC=<容器名或宿主机目录> 00b_sync_motor_patch.sh
#   例:   MOTOR_PATCH_SRC=longpause-motor scripts/00b_sync_motor_patch.sh
#
# 源可以是：
#   - 一个已存在且包含补丁包的容器名（默认寻找 longpause-motor）
#   - 宿主机目录（其下应有 motor/ 或直接是 site-packages/motor）
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_container "$MOTOR_CONTAINER"

SP="/usr/local/python3.12.13/lib/python3.12/site-packages"
SRC="${MOTOR_PATCH_SRC:-longpause-motor}"
TMPHOST="$(mktemp -d /tmp/motor_patch.XXXXXX)"

if container_exists "$SRC"; then
  say "从容器 $SRC 取补丁包"
  docker cp "$SRC:$SP/motor" "$TMPHOST/motor" >/dev/null || die "从 $SRC 拷贝失败"
elif [ -d "$SRC/motor" ]; then
  say "从宿主机目录 $SRC 取补丁包"
  cp -a "$SRC/motor" "$TMPHOST/motor"
elif [ -d "$SRC" ] && [ -f "$SRC/coordinator/domain/agent_hint.py" ]; then
  say "从宿主机目录 $SRC（直接是 motor/）取补丁包"
  cp -a "$SRC" "$TMPHOST/motor"
else
  die "找不到补丁源：$SRC（设置 MOTOR_PATCH_SRC 为容器名或目录）"
fi

grep -q '_AGENT_STATUS_STATES_ALLOWED' "$TMPHOST/motor/coordinator/domain/agent_hint.py" \
  || die "补丁源不含 resume 白名单"
grep -q 'internal/agent-status' "$TMPHOST/motor/coordinator/api_server/observability_server.py" \
  || die "补丁源不含 /internal/agent-status 路由"

docker cp "$TMPHOST/motor" "$MOTOR_CONTAINER:/tmp/motor_patched" >/dev/null
docker exec "$MOTOR_CONTAINER" sh -c "
  rm -rf $SP/motor && mv /tmp/motor_patched $SP/motor
  find $SP/motor -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
"
rm -rf "$TMPHOST"
say "安装 grpcio>=1.84.1 ..."
docker exec "$MOTOR_CONTAINER" sh -c "pip install -q 'grpcio>=1.84.1'" >/dev/null 2>&1 || warn "grpcio 升级失败（离线时请自备 wheel）"
say "完成：motor 补丁包已同步。随后 make motor ARM=<arm> 重起 coordinator 生效。"
