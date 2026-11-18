#!/bin/bash
# 04 跑单轮（并记录引擎纪元快照，供 V9「基线洁净度」判定）
#   用法: 04_run_one.sh <dataset> <arm> [repeat] [lane] [其余 driver 参数...]
#
# 重要：切臂（尤其 decl -> off）必须先重启引擎进程，否则 off 基线会被
#       hints-on 流量留下的引擎状态（会话注册表/受保护块/空闲计数虚高）污染。
#       本脚本不替你重启引擎，只负责记录「本轮引擎上此前是否有 hint 流量」。
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DS="${1:?用法: 04_run_one.sh <dataset> <arm> [repeat] [lane]}"
ARM="${2:?缺少 arm}"
REP="${3:-1}"
LANE="${4:-A}"
shift 4 2>/dev/null || shift $#
EXTRA=("$@")

DRV="$SCENARIO_ROOT/driver/replay_driver.py"
[ -f "$DRV" ] || die "缺少 driver: $DRV"

EPID="$(engine_pid)"
[ -n "$EPID" ] || warn "没找到引擎进程（pgrep 'vllm serve'）；V9 将无法判定"
if [ -f "$ENGINE_EPOCH" ] && [ -n "$EPID" ]; then
  EPOCH_PID=$(python3 -c "import json;print(json.load(open('$ENGINE_EPOCH')).get('pid',''))" 2>/dev/null || true)
  [ -n "$EPOCH_PID" ] && [ "$EPOCH_PID" != "$EPID" ] && \
    warn "引擎 pid 与 engine-epoch.json 不一致（epoch=$EPOCH_PID 当前=$EPID）——引擎在 02 之外被重启过？请重跑 02_start_engine.sh 记录纪元"
fi
ESTART="$(engine_started "$EPID")"
EPSTART_TS="$(python3 -c "import json;print(json.load(open('$ENGINE_EPOCH')).get('started_ts',''))" 2>/dev/null || true)"
HINT_BEFORE=0
[ -n "$EPID" ] && HINT_BEFORE="$(hint_seen_on_engine "$EPID" "$EPSTART_TS" "${ENGINE_IMAGE_ID:-}")"

if [ "$ARM" = "off" ] && [ "$HINT_BEFORE" = "1" ]; then
  warn "当前引擎实例(pid=$EPID)此前跑过 hints-on 流量 ⇒ 本 off 轮的基线已被污染（预计 p50 劣化到 ~1s 级）。"
  warn "要得到干净 off 基线，请先执行: scripts/02_start_engine.sh --restart"
fi

BEFORE_TS=$(date +%s)
FORM="${AH_FORM:-clean}"
RLOG="$LOG_DIR/run-$ARM-$FORM-$(date +%H%M%S).log"
say "run: dataset=$DS arm=$ARM form=$FORM repeat=$REP lane=$LANE engine_pid=${EPID:-?}"
python3 "$DRV" \
  --dataset "$DS" --arm "$ARM" --lane "$LANE" \
  --anchor pause --reset-prefix-cache --kvc-ticks --hint-mode header \
  --repeat "$REP" --out-root "$AH_EVIDENCE_ROOT" \
  "${EXTRA[@]}" 2>&1 | tee -a "$RLOG"

# 精确绑定本轮 evidence：解析 driver 收尾 JSON 的 evidence_dir（不按 mtime 反查，
# 避免并发跑轮时取到别人的目录）。
RUN_DIR="$(python3 - "$RLOG" <<'PYRUN'
import json, re, sys
buf = open(sys.argv[1], encoding="utf-8", errors="replace").read()
best = ""
for m in re.finditer(r'"evidence_dir"\s*:\s*"([^"]+)"', buf):
    best = m.group(1)          # 取最后一个（收尾摘要）
print(best)
PYRUN
)"
if [ -z "$RUN_DIR" ]; then
  # 兜底（旧版 driver 未打印 evidence_dir）：按 mtime 反查
  RUN_DIR="$(python3 - "$AH_EVIDENCE_ROOT" "$BEFORE_TS" <<'PYRUN'
import os, sys
root, t0 = sys.argv[1], float(sys.argv[2])
cands = []
for d in os.listdir(root):
    p = os.path.join(root, d)
    if os.path.isdir(p) and os.path.isfile(os.path.join(p, "meta.json")) and os.path.getmtime(p) >= t0 - 2:
        cands.append((os.path.getmtime(p), p))
print(max(cands)[1] if cands else "")
PYRUN
)"
  [ -n "$RUN_DIR" ] && warn "driver 未输出 evidence_dir，已按 mtime 兜底绑定: $RUN_DIR"
fi
[ -n "$RUN_DIR" ] || { warn "没找到本轮 evidence 目录（可能是 --dry-run）"; exit 0; }

python3 - "$RUN_DIR/_run_meta.json" "$ARM" "$EPID" "$ESTART" "$HINT_BEFORE" "$BEFORE_TS" "$ENGINE_EPOCH" "$FORM" "${ENGINE_IMAGE_ID:-}" <<'PY'
import json, sys
out, arm, pid, started, hint_before, t0, epoch_file, form, image_id = sys.argv[1:10]
ep = {}
try:
    ep = json.load(open(epoch_file, encoding="utf-8"))
except Exception:
    pass
d = {"arm": arm,
     "form": form,
     "engine_pid": int(pid) if str(pid).isdigit() else pid,
     "engine_started_human": started,
     "engine_started_ts": ep.get("started_ts"),
     "engine_image_id": image_id or ep.get("image_id"),
     "pool_blocks": ep.get("pool_blocks"),
     "hint_traffic_on_engine_before": bool(int(hint_before or 0)),
     "run_launch_ts": float(t0)}
json.dump(d, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
record_arm_history "$ARM" "$RUN_DIR" "$FORM"
say "evidence: $RUN_DIR（form=$FORM；此引擎此前 hint 流量=$HINT_BEFORE）"
