#!/bin/bash
# 00 前置检查：镜像/模型/卡/数据/驱动/motor 代码版本。任何 FAIL 都不要继续。
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

FAIL=0
ok()   { printf '  [ OK ] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=1; }
note() { printf '  [note] %s\n' "$*"; }

say "场景根目录: $SCENARIO_ROOT"
say "环境文件: $([ -f "$SCENARIO_ROOT/env/env.sh" ] && echo env/env.sh || echo 'env/env.example.sh(默认)')"

say "== 工具 =="
command -v docker >/dev/null && ok "docker" || bad "docker 未安装"
command -v curl   >/dev/null && ok "curl"   || bad "curl 未安装"
command -v python3>/dev/null && ok "python3"|| bad "python3 未安装"

say "== 镜像（按 ID 校验，同名 tag 不算数） =="
for pair in "$ENGINE_IMAGE|$ENGINE_IMAGE_ID" "$MOTOR_IMAGE|$MOTOR_IMAGE_ID"; do
  img=${pair%%|*}; want=${pair##*|}
  got=$(docker image inspect -f '{{.Id}}' "$img" 2>/dev/null || true)
  if [ -z "$got" ]; then
    bad "镜像缺失: $img"
  elif [ "$got" = "$want" ]; then
    ok "$img  $got"
  else
    bad "镜像 ID 不符: $img 期望 $want 实际 $got"
  fi
done

say "== 模型 =="
if [ -d "$MODEL_PATH" ] && [ -f "$MODEL_PATH/config.json" ]; then
  ok "$MODEL_PATH (served as $SERVED_MODEL_NAME)"
else
  bad "模型目录/配置缺失: $MODEL_PATH"
fi

say "== 卡 =="
if command -v npu-smi >/dev/null; then
  if npu-smi info 2>/dev/null | grep -q "910B"; then ok "npu-smi 可见 910B"; else warn "npu-smi 未见 910B"; fi
  note "NPU_INDEX=$NPU_INDEX（请自行确认没有别的进程独占该卡）"
else
  warn "npu-smi 不可用（容器内/无驱动环境可忽略）"
fi

say "== 数据（生成后校验 sha256） =="
if [ -f "$SCENARIO_ROOT/data/generated/contend_sp7_p60_S8.jsonl" ]; then
  python3 - "$SCENARIO_ROOT" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
spec = json.load(open(os.path.join(root, "config", "scenario.json")))
bad = 0
for name, want in spec["dataset"]["sha256"].items():
    p = os.path.join(root, "data", "generated", name)
    if not os.path.isfile(p):
        print("  [FAIL] 缺失 %s" % name); bad = 1; continue
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != want:
        print("  [FAIL] sha256 不符 %s\n         期望 %s\n         实际 %s" % (name, want, h)); bad = 1
    else:
        print("  [ OK ] %s  %s" % (name, h[:16] + "..."))
sys.exit(1 if bad else 0)
PY
  [ $? -eq 0 ] || FAIL=1
else
  note "尚未生成数据：先跑 make data（data/generated/ 不入 git）"
fi

say "== 驱动 =="
DRV="$SCENARIO_ROOT/driver/replay_driver.py"
[ -f "$DRV" ] && ok "driver/replay_driver.py  sha256=$(sha256sum "$DRV" | cut -c1-16)..." || bad "缺少 driver"

say "== motor 代码版本（11 号坑：镜像 ID 相同 ≠ 容器内代码相同） =="
if container_exists "$MOTOR_CONTAINER"; then
  ah="$($(echo docker) exec "$MOTOR_CONTAINER" sh -c 'ls /usr/local/python3.12.13/lib/python3.12/site-packages/motor/coordinator/domain/agent_hint.py 2>/dev/null || find / -name agent_hint.py -path "*motor*" 2>/dev/null | head -1' 2>/dev/null | tr -d '\r')"
  if [ -z "$ah" ]; then
    bad "找不到容器内 motor/coordinator/domain/agent_hint.py"
  elif docker exec "$MOTOR_CONTAINER" sh -c "grep -q '_AGENT_STATUS_STATES_ALLOWED' '$ah'" 2>/dev/null; then
    sp=$(dirname "$(dirname "$(dirname "$(dirname "$ah")")")")
    # 补丁包还必须带 obs 口的 /internal/agent-status 别名（stock 镜像没有）
    if docker exec "$MOTOR_CONTAINER" sh -c "grep -q 'internal/agent-status' '$sp/motor/coordinator/api_server/observability_server.py'" 2>/dev/null; then
      ok "motor 为补丁版（resume 白名单 + /internal/agent-status）: $ah"
    else
      bad "motor 补丁不全：缺 /internal/agent-status 路由（stock 镜像无此路由，状态 POST 会 404）。跑 scripts/00b_sync_motor_patch.sh"
    fi
    gv=$(docker exec "$MOTOR_CONTAINER" sh -c "pip show grpcio 2>/dev/null | sed -n 's/^Version: //p'" | tr -d '\r')
    if [ -n "${gv:-}" ] && python3 -c "import sys;v='$gv'.split('.');sys.exit(0 if tuple(map(int,v[:2]))>=(1,84) else 1)" 2>/dev/null; then
      ok "grpcio=$gv（补丁包要求 >=1.84.1）"
    else
      bad "grpcio=${gv:-?} 过低：补丁包生成代码要求 >=1.84.1（pip install 'grpcio>=1.84.1'）"
    fi
  else
    bad "motor 为镜像原版：状态白名单没有 resume ⇒ resume 会被拒（hit/pt 掉到 43-48%）。跑 scripts/00b_sync_motor_patch.sh"
  fi
else
  note "motor 容器不存在（$MOTOR_CONTAINER）：先跑 scripts/01_create_containers.sh"
fi

say "== 端口现状 =="
for p in "$INFER_PORT" "$MGMT_PORT" "$OBS_PORT" "$ENGINE_PORT"; do
  note "port $p  http=$(http "http://127.0.0.1:$p/" 3)"
done

echo
if [ "$FAIL" -ne 0 ]; then
  say "preflight: FAIL"
  exit 1
fi
say "preflight: PASS"
