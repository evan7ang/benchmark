#!/bin/bash
# 05 按 config/scenario.json 的矩阵跑多轮（含容量准入、数据 sha 校验、形态隔离）
#
#   用法: 05_run_matrix.sh [--dry-run] [--force] [--matrix default|churn|churn-all|mixed]
#     default   = forms clean 的最小对照（S8 off/decl 各 1 轮）
#     churn     = 形态 C 主证据（L2 off×2 + decl×2）
#     churn-all = 三档压力扫描（L1/L2/L3 各 off/decl 1 轮）
#     mixed     = 形态 B（decl 后不重启接 off，V9 预期 FAIL，仅归档）
#   --force: 忽略容量准入（比值 > 1.05 也继续）
#
#   形态语义（config/scenario.json -> forms）：
#     clean/churn: 每臂/每轮重启引擎，两臂都是干净基线
#     mixed      : 刻意不重启引擎（生产混部形态）
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=0; FORCE=0; WHICH=default
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --matrix) WHICH="${2:?--matrix 需要值}"; shift ;;
    --matrix=*) WHICH="${1#*=}" ;;
    *) die "未知参数 $1" ;;
  esac
  shift
done

SPEC="$SCENARIO_ROOT/config/scenario.json"
[ -f "$SPEC" ] || die "缺少 $SPEC"

case "$WHICH" in
  default)   MKEY="matrix" ;;
  churn)     MKEY="matrix_churn" ;;
  churn-all) MKEY="matrix_churn_all" ;;
  mixed)     MKEY="matrix_mixed" ;;
  *) die "--matrix 只能是 default|churn|churn-all|mixed" ;;
esac
say "矩阵集: $WHICH（scenario.json -> $MKEY）"

# 1) 数据 sha256 校验（只校验本矩阵用到的数据集）
say "校验数据 sha256 ..."
python3 - "$SCENARIO_ROOT" "$MKEY" <<'PY' || exit 1
import hashlib, json, os, sys
root, mkey = sys.argv[1], sys.argv[2]
spec = json.load(open(os.path.join(root, "config", "scenario.json")))
names = {m["dataset"] for m in spec.get(mkey) or []}
bad = 0
for name in sorted(names):
    want = (spec["dataset"].get("sha256") or {}).get(name)
    p = os.path.join(root, "data", "generated", name)
    if not os.path.isfile(p):
        print("  [FAIL] 缺少 %s（先 make data）" % name); bad = 1; continue
    if not want:
        print("  [WARN] %s 未在 scenario.json 登记 sha256（跳过校验）" % name); continue
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if h != want:
        print("  [FAIL] %s sha256 不符（期望 %s…）" % (name, want[:12])); bad = 1
    else:
        print("  [ OK ] %s" % name)
print("  数据校验 %s" % ("PASS" if not bad else "FAIL"))
sys.exit(1 if bad else 0)
PY

# 2) 容量准入（churn 数据集只按 agent 会话数算保护需求）
POOL="$(pool_blocks)"
say "池子块数: ${POOL:-未知}（来自 results/logs/pool.env 或 POOL_BLOCKS_OVERRIDE）"
if [ -z "$POOL" ]; then
  warn "未知池子块数：容量准入跳过（先跑 02_start_engine.sh）"
fi
python3 - "$SPEC" "$POOL" "$FORCE" "$MKEY" <<'PY' || exit 1
import json, re, sys
spec = json.load(open(sys.argv[1]))
pool, force, mkey = sys.argv[2].strip(), sys.argv[3] == "1", sys.argv[4]
bps = spec["dataset"]["blocks_per_session"]
if not pool:
    print("  [skip] 容量准入（池子未知）"); sys.exit(0)
pool = int(pool)
bad, seen = 0, set()
for m in spec.get(mkey) or []:
    name = m["dataset"]
    mt = re.search(r"_S(\d+)", name)
    n = int(mt.group(1)) if mt else None
    if n is None or (name, n) in seen:
        continue
    seen.add((name, n))
    need = n * bps
    ratio = need / pool
    ok = ratio <= 1.05
    print("  [%s] %-38s agent=%2d 需求=%4d 池子=%4d 比值=%.2f" % (
        "OK  " if ok else "OVER", name, n, need, pool, ratio))
    if not ok:
        bad = 1
if bad and not force:
    print("  容量准入 FAIL：比值 > 1.05，hint 会失效/反向。确需跑请加 --force")
    sys.exit(1)
if bad:
    print("  [force] 容量不足仍继续")
PY

# 3) 计划
PLAN="$LOG_DIR/matrix-plan-$WHICH.txt"
python3 - "$SPEC" "$MKEY" <<'PY' > "$PLAN"
import json, sys
spec = json.load(open(sys.argv[1]))
for m in spec.get(sys.argv[2]) or []:
    print("%s|%s|%s|%s" % (m["dataset"], m["arm"], m.get("repeat", 1), m.get("form", "clean")))
PY
say "计划："
sed 's/^/  /' "$PLAN"

if [ "$DRY" -eq 1 ]; then
  say "--dry-run：不实际打流"
  exit 0
fi

FAILED=0
PREV_ARM=""
PREV_FORM=""
FIRST=1
while IFS='|' read -r DS ARM REP FORM; do
  [ -n "${DS:-}" ] || continue
  say "==> $DS $ARM form=$FORM (repeat=$REP)"

  RS="$(python3 -c "import json;print(json.load(open('$SPEC')).get('forms',{}).get('$FORM',{}).get('restart','per_arm'))" 2>/dev/null || echo per_arm)"
  NEED_RESTART=0
  case "$FORM" in
    mixed)
      # 生产混部：只在序列开头重启一次，之后刻意不重启
      [ "$FIRST" = "1" ] && NEED_RESTART=1
      ;;
    *)
      case "$RS" in
        per_round) NEED_RESTART=1 ;;
        per_arm)   [ "$ARM" != "$PREV_ARM" ] && NEED_RESTART=1 ;;
        never)     NEED_RESTART=0 ;;
      esac
      # off 轮永远不能踩在「跑过 hint 流量」的引擎实例上
      if [ "$ARM" = "off" ] && [ "$(hint_seen_on_engine "$(engine_pid)")" = "1" ]; then
        NEED_RESTART=1
      fi
      ;;
  esac
  if [ "$NEED_RESTART" = "1" ]; then
    say "重启引擎进程（form=$FORM, $PREV_FORM/$PREV_ARM -> $FORM/$ARM）"
    bash "$SCENARIO_ROOT/scripts/02_start_engine.sh" --restart || { warn "重启引擎失败"; FAILED=1; break; }
  fi

  if ! bash "$SCENARIO_ROOT/scripts/03_start_motor.sh" "$ARM"; then
    warn "起 motor 失败：$DS $ARM"; FAILED=1; break
  fi
  for i in $(seq 1 "$REP"); do
    say "  round $i/$REP"
    if ! AH_FORM="$FORM" bash "$SCENARIO_ROOT/scripts/04_run_one.sh" "$DS" "$ARM" "$i" "A"; then
      warn "跑流失败：$DS $ARM round=$i"; FAILED=1; break 2
    fi
    if ! python3 "$SCENARIO_ROOT/scripts/07_verify.py" --latest --arm "$ARM" --form "$FORM" \
         --dataset-prefix "$(basename "$DS" .jsonl)" --quiet; then
      if [ "$FORM" = "mixed" ]; then
        warn "验收未通过（form=mixed 的 off 轮 V9 预期 FAIL；结论归入「生产混部形态」，不作主结论）"
      else
        warn "验收未通过：$DS $ARM round=$i"; FAILED=1; break 2
      fi
    fi
  done
  PREV_ARM="$ARM"; PREV_FORM="$FORM"; FIRST=0
done < "$PLAN"

[ "$FAILED" -eq 0 ] || die "矩阵存在失败轮次"
say "矩阵完成。分析：make analyze"
