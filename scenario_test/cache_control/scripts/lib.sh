#!/bin/bash
# 公共函数：所有脚本 source 本文件。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export SCENARIO_ROOT

if [ -f "$SCENARIO_ROOT/env/env.sh" ]; then
  # shellcheck source=/dev/null
  . "$SCENARIO_ROOT/env/env.sh"
else
  # shellcheck source=/dev/null
  . "$SCENARIO_ROOT/env/env.example.sh"
fi

mkdir -p "$LOG_DIR" "$AH_EVIDENCE_ROOT"

say()  { echo "[$(date '+%F %T')] $*"; }
warn() { echo "[$(date '+%F %T')] WARN: $*" >&2; }
die()  { echo "[$(date '+%F %T')] ERROR: $*" >&2; exit 1; }

http() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-3}" "$1" 2>/dev/null || true; }

wait_until_http() { # url expected timeout_s
  local url="$1" exp="$2" t="${3:-60}" i
  for i in $(seq 1 $(( t / 2 ))); do
    [ "$(http "$url")" = "$exp" ] && return 0
    sleep 2
  done
  return 1
}

container_exists() { docker inspect "$1" >/dev/null 2>&1; }

require_container() {
  container_exists "$1" || die "容器不存在: $1（先跑 scripts/01_create_containers.sh）"
}

pool_blocks() {
  # 优先读 02_start_engine.sh 落下的 pool.env；否则读 env 里的 override；再否则空
  if [ -f "$LOG_DIR/pool.env" ]; then
    # shellcheck source=/dev/null
    . "$LOG_DIR/pool.env"
    [ -n "${POOL_BLOCKS:-}" ] && { echo "$POOL_BLOCKS"; return 0; }
  fi
  [ -n "${POOL_BLOCKS_OVERRIDE:-}" ] && { echo "$POOL_BLOCKS_OVERRIDE"; return 0; }
  echo ""
}

# ---- 引擎进程纪元（用于「臂隔离」判据）--------------------------------------
ENGINE_EPOCH="$LOG_DIR/engine-epoch.json"
ARM_HISTORY="$LOG_DIR/arm-history.jsonl"

engine_pid() {
  # 取真正的 python 引擎进程（避开 `bash -lc` 包装进程）。
  # 注意：vLLM v1 会 setproctitle 成 `VLLM::EngineCore`，不能只匹配 'vllm serve'。
  docker exec "$ENGINE_CONTAINER" sh -c '
    for p in $(pgrep -f "vllm serv[e]|[Q]wen3-8B" 2>/dev/null); do
      st=$(awk "{print \$3}" /proc/$p/stat 2>/dev/null || true)
      [ "$st" = "Z" ] && continue                      # 跳过僵尸
      exe=$(readlink /proc/$p/exe 2>/dev/null || true)
      case "$exe" in *python*) echo "$p"; break;; esac
    done' 2>/dev/null | tr -d '\r'
}

# 等引擎 HBM 真正释放（SIGKILL 后 NPU 驱动可能延迟回收）。
# 用 npu-smi 的进程账，不用 HBM 总量（基线本身就有 ~2.8 GB 驱动占用）。
wait_engine_hbm_released() { # timeout_s(default 60)
  local t="${1:-60}" i n
  for i in $(seq 1 $(( t / 2 ))); do
    n=$(npu-smi info -t proc-mem -i "${NPU_INDEX:-0}" 2>/dev/null | grep -c 'Process id' || true)
    [ "${n:-1}" = "0" ] && return 0
    sleep 2
  done
  warn "等待 HBM 释放超时（仍有 ${n:-?} 个 NPU 进程持卡）"
  return 1
}

engine_started() {
  local pid="${1:-$(engine_pid)}"
  [ -n "$pid" ] || { echo ""; return; }
  docker exec "$ENGINE_CONTAINER" sh -c "ps -o lstart= -p $pid" 2>/dev/null | sed 's/^ *//' | tr -d '\r'
}

write_engine_epoch() { # pid started tokens blocks
  python3 - "$ENGINE_EPOCH" "$ENGINE_CONTAINER" "$ENGINE_IMAGE_ID" "$@" <<'PY'
import datetime, json, sys
out, container, image_id = sys.argv[1:4]
pid, started, tok, blk = (sys.argv[4:8] + ["", "", "", ""])[:4]
def to_epoch(s):
    try:
        return datetime.datetime.strptime(s, "%a %b %d %H:%M:%S %Y").timestamp()
    except Exception:
        return None
d = {"container": container, "image_id": image_id, "pid": int(pid) if str(pid).isdigit() else pid,
     "started_human": started, "started_ts": to_epoch(started),
     "pool_tokens": int(tok) if str(tok).isdigit() else tok,
     "pool_blocks": int(blk) if str(blk).isdigit() else blk,
     "recorded_ts": datetime.datetime.now().timestamp()}
json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

# 返回该引擎实例上是否跑过「会发 hint」的臂（off 之外都算）。
# 用 (image_id, started_ts) 作实例主键，(pid) 仅作退化兜底：
# 容器重启后 PID 可能复用，只看 pid 会误判「干净」。
hint_seen_on_engine() { # pid [started_ts] [image_id]
  local pid="$1" started="${2:-}" image="${3:-${ENGINE_IMAGE_ID:-}}"
  [ -f "$ARM_HISTORY" ] || { echo 0; return; }
  python3 - "$ARM_HISTORY" "$pid" "$started" "$image" <<'PY'
import json, sys
path, pid, started, image = sys.argv[1], str(sys.argv[2]), sys.argv[3], sys.argv[4]
seen = 0
try:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("arm") == "off":
            continue
        same = False
        if started and r.get("engine_started_ts"):
            same = str(r.get("engine_started_ts")) == str(started) and \
                   (not image or not r.get("engine_image_id") or r.get("engine_image_id") == image)
        if not same and not started:
            same = str(r.get("engine_pid")) == pid
        if same:
            seen = 1
            break
except FileNotFoundError:
    pass
print(seen)
PY
}

record_arm_history() { # arm run_dir [form]
  python3 - "$ARM_HISTORY" "$ENGINE_EPOCH" "$1" "$2" "${3:-clean}" "${ENGINE_IMAGE_ID:-}" <<'PY'
import datetime, json, os, sys
path, epoch_file, arm, run_dir, form, image_id = sys.argv[1:7]
ep = {}
try:
    ep = json.load(open(epoch_file, encoding="utf-8"))
except Exception:
    pass
rec = {"ts": datetime.datetime.now().timestamp(), "arm": arm, "run_dir": run_dir,
       "form": form,
       "engine_pid": ep.get("pid"), "engine_started_ts": ep.get("started_ts"),
       "engine_image_id": image_id or ep.get("image_id"),
       "engine_container": ep.get("container")}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}

# 防误伤：ttft-* 前缀被视为「可能属于别人的共享容器」，默认拒绝任何写操作
guard_container() { # $1=container name
  case "${1:-}" in
    ttft-*)
      if [ "${ALLOW_SHARED_CONTAINERS:-0}" != "1" ]; then
        die "拒绝操作容器 '$1'（疑似共享/他人容器）。确需操作请显式设置 ALLOW_SHARED_CONTAINERS=1"
      fi
      warn "ALLOW_SHARED_CONTAINERS=1：允许操作共享容器 '$1'"
      ;;
  esac
}
