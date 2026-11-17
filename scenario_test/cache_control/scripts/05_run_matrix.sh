#!/bin/bash
# 05 按 config/scenario.json 的 matrix 跑多轮（含容量准入与数据 sha 校验）
#   用法: 05_run_matrix.sh [--dry-run] [--force]
#   --force: 忽略容量准入（比值 > 1.05 也继续）
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0; FORCE=0
for a in "$@"; do
  case "$a" in --dry-run) DRY=1;; --force) FORCE=1;; *) die "未知参数 $a";; esac
done

SPEC="$SCENARIO_ROOT/config/scenario.json"
[ -f "$SPEC" ] || die "缺少 $SPEC"

# 1) 数据 sha256 校验
say "校验数据 sha256 ..."
python3 - "$SCENARIO_ROOT" <<'PY' || exit 1
import hashlib, json, os, sys
root = sys.argv[1]
spec = json.load(open(os.path.join(root, "config", "scenario.json")))
bad = 0
for name, want in spec["dataset"]["sha256"].items():
    p = os.path.join(root, "data", "generated", name)
    if not os.path.isfile(p):
        print("  [FAIL] 缺少 %s（先 make data）" % name); bad = 1; continue
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != want:
        print("  [FAIL] %s sha256 不符" % name); bad = 1
print("  数据校验 %s" % ("PASS" if not bad else "FAIL"))
sys.exit(1 if bad else 0)
PY

# 2) 容量准入
POOL="$(pool_blocks)"
say "池子块数: ${POOL:-未知}（来自 results/logs/pool.env 或 POOL_BLOCKS_OVERRIDE）"
if [ -z "$POOL" ]; then
  warn "未知池子块数：容量准入跳过（先跑 02_start_engine.sh）"
fi
python3 - "$SPEC" "$POOL" "$FORCE" <<'PY' || exit 1
import json, sys
spec = json.load(open(sys.argv[1]))
pool = sys.argv[2].strip()
force = sys.argv[3] == "1"
bps = spec["dataset"]["blocks_per_session"]
if not pool:
    print("  [skip] 容量准入（池子未知）"); sys.exit(0)
pool = int(pool)
bad = 0
seen = set()
for m in spec["matrix"]:
    n = int(m["dataset"].rsplit("_S", 1)[1].split(".")[0])
    need = n * bps
    ratio = need / pool if pool else 0
    if (m["dataset"], n) in seen: continue
    seen.add((m["dataset"], n))
    ok = ratio <= 1.05
    tag = "OK  " if ok else "OVER"
    print("  [%s] %-28s 会话=%2d 需求=%4d 池子=%4d 比值=%.2f" % (tag, m["dataset"], n, need, pool, ratio))
    if not ok:
        bad = 1
if bad and not force:
    print("  容量准入 FAIL：比值 > 1.05，hint 会失效/反向。确需跑请加 --force")
    sys.exit(1)
if bad:
    print("  [force] 容量不足仍继续")
PY

# 3) 跑矩阵
say "矩阵："
python3 - "$SPEC" <<'PY' > "$LOG_DIR/matrix-plan.txt"
import json, sys
spec = json.load(open(sys.argv[1]))
for m in spec["matrix"]:
    print("%s|%s|%s" % (m["dataset"], m["arm"], m.get("repeat", 1)))
PY
cat "$LOG_DIR/matrix-plan.txt" | sed 's/^/  /'

if [ "$DRY" -eq 1 ]; then
  say "--dry-run：不实际打流"
  exit 0
fi

FAILED=0
PREV_ARM=""
ISO=$(python3 -c "import json;print(json.load(open('$SPEC')).get('isolation',{}).get('engine_restart','per_arm'))" 2>/dev/null || echo per_arm)
say "臂隔离策略: engine_restart=$ISO"
while IFS='|' read -r DS ARM REP; do
  [ -n "${DS:-}" ] || continue
  say "==> $DS $ARM (repeat=$REP)"

  # ---- 臂隔离：切臂必须重启引擎进程 ----
  NEED_RESTART=0
  case "$ISO" in
    per_round) NEED_RESTART=1 ;;
    per_arm)   [ "$ARM" != "$PREV_ARM" ] && NEED_RESTART=1 ;;
    never)     NEED_RESTART=0 ;;
  esac
  # off 轮永远不能踩在「跑过 hint 流量」的引擎实例上
  if [ "$ARM" = "off" ] && [ "$(hint_seen_on_engine "$(engine_pid)")" = "1" ]; then
    NEED_RESTART=1
  fi
  if [ "$NEED_RESTART" = "1" ]; then
    say "重启引擎进程（臂隔离：$PREV_ARM -> $ARM）"
    bash "$SCENARIO_ROOT/scripts/02_start_engine.sh" --restart || { warn "重启引擎失败"; FAILED=1; break; }
  fi

  if ! bash "$SCENARIO_ROOT/scripts/03_start_motor.sh" "$ARM"; then
    warn "起 motor 失败：$DS $ARM"; FAILED=1; break
  fi
  for i in $(seq 1 "$REP"); do
    say "  round $i/$REP"
    if ! bash "$SCENARIO_ROOT/scripts/04_run_one.sh" "$DS" "$ARM" "$i" "A"; then
      warn "跑流失败：$DS $ARM round=$i"; FAILED=1; break 2
    fi
    # 单轮证据验收（V2 至少 http_errors=0）
    if ! python3 "$SCENARIO_ROOT/scripts/07_verify.py" --latest --arm "$ARM" --dataset-prefix contend_sp7_p60 --quiet; then
      warn "验收未通过：$DS $ARM round=$i"; FAILED=1; break 2
    fi
  done
  PREV_ARM="$ARM"
done < "$LOG_DIR/matrix-plan.txt"

[ "$FAILED" -eq 0 ] || die "矩阵存在失败轮次"
say "矩阵完成。分析：make analyze"
