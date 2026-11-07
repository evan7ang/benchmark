#!/usr/bin/env bash
# Single QA entrypoint for KVC functional testing.
#
#   qa_run.sh doctor              # environment preflight
#   qa_run.sh datasets [--fast]   # build + validate datasets
#   qa_run.sh all [--fast]        # run all S1-S7 and write QA_REPORT.md/html
#   qa_run.sh case s3 [--fast]    # run one scenario
#   qa_run.sh observe             # live KV/KVC observation (Ctrl-C to exit)
#   qa_run.sh stop                # stop the isolated stack
#
# --fast uses idle_offload_s=10, delay=15s, 3-session datasets (about 20-30 min
# for all cases). Without --fast it uses idle=30s, delay=45s (about 60-90 min).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/common.sh"

CMD="${1:-help}"
shift || true
FAST=0
for arg in "$@"; do
    [ "$arg" = "--fast" ] && FAST=1
done

MODE="standard"
IDLE_OFFLOAD="$STD_IDLE_OFFLOAD"
S1_DURATION="$STD_S1_DURATION"
S5_DURATION="$STD_S5_DURATION"
MODEL="kvc_manual_c8"
MODEL_GATE="kvc_manual_c1"
DATASET_FUNC="kvc_manual_pause_offload_quick"
DATASET_FIN="kvc_manual_finalize_quick"
DATASET_GATE="kvc_manual_gate_quick"
DATASET_SAT_PAUSE="kvc_manual_saturation_pause"

if [ "$FAST" = "1" ]; then
    MODE="fast"
    IDLE_OFFLOAD="$FAST_IDLE_OFFLOAD"
    S1_DURATION="$FAST_S1_DURATION"
    S5_DURATION="$FAST_S5_DURATION"
    DATASET_FUNC="kvc_manual_pause_offload_fast"
    DATASET_FIN="kvc_manual_finalize_fast"
    DATASET_GATE="kvc_manual_gate_fast"
    DATASET_SAT_PAUSE="kvc_manual_saturation_pause_fast"
fi

usage() {
    cat <<EOF
KVC 手工测试统一入口

  $0 init                   从 example 生成 kvc_test.env
  $0 doctor                 环境预检
  $0 datasets [--fast]      生成并 tokenizer 校验数据集
  $0 selftest               静态检查硬编码路径/语法
  $0 prepare-motor          source 模式下构建 Motor wheel
  $0 bundle                 打包交付物
  $0 all [--fast]           串跑 S1-S7，生成 QA_REPORT.md / .html
  $0 case <s1|s2|s3|s5|s6|s7> [--fast]
  $0 observe                实时观察 KV usage / Waiting / KVC 计数
  $0 report                 重新生成最近一次 all/case 的汇总报告
  $0 stop                   停止隔离栈

环境变量：
  KEEP_STACK=1  all/case 结束后不停止隔离栈
  CARD=<n>      指定 NPU 卡（默认 $CARD）
EOF
}

QA_DIR=""
MANIFEST=""

new_run_dir() {
    QA_DIR="$RESULTS_DIR/$(date +%Y%m%d_%H%M%S)_qa_${MODE}"
    mkdir -p "$QA_DIR"
    MANIFEST="$QA_DIR/manifest.jsonl"
    : > "$MANIFEST"
    python3 - "$QA_DIR/meta.json" "$MODE" "$CARD" "$ENGINE_PORT" \
        "$MOTOR_INFER_PORT" "$MOTOR_MGMT_PORT" "$MOTOR_OBS_PORT" <<'PY'
import json, sys
path, mode, card, engine, infer, mgmt, obs = sys.argv[1:]
json.dump({
    "mode": mode, "card": card, "engine_port": engine,
    "motor_ports": f"{infer}-{obs}", "motor_infer": infer,
    "motor_mgmt": mgmt, "motor_obs": obs,
}, open(path, "w"), indent=2)
PY
}

append_manifest() {
    python3 - "$MANIFEST" "$1" "$2" "$3" "$4" <<'PY'
import json, sys
path, case_id, name, expected, result_dir = sys.argv[1:]
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "case_id": case_id, "name": name, "expected": expected,
        "result_dir": result_dir,
    }, ensure_ascii=False) + "\n")
PY
}

ensure_stack() {
    local kvc="$1" idle="$2" high="$3" prefetch="$4"
    if curl -sf --max-time 3 "http://${HOST_IP}:${ENGINE_PORT}/v1/models" >/dev/null 2>&1; then
        bash "$KIT_DIR/scripts/start_stack.sh" --kvc "$kvc" --idle-offload "$idle" \
            --high-water "$high" --prefetch "$prefetch" --no-engine --no-meta
    else
        bash "$KIT_DIR/scripts/start_stack.sh" --kvc "$kvc" --card "$CARD" \
            --port "$ENGINE_PORT" --idle-offload "$idle" --high-water "$high" --prefetch "$prefetch"
    fi
}

run_one() {
    local case_id="$1" name="$2" expected="$3" model="$4" dataset="$5" duration="${6:-}"
    log "===== ${case_id} ${name} ====="
    rm -f "$RESULTS_DIR/.last_result_dir"
    local args=(--case "$case_id" --model "$model" --dataset "$dataset")
    [ -n "$duration" ] && args+=(--duration "$duration")
    bash "$KIT_DIR/scripts/run_case.sh" "${args[@]}" || true
    local dir
    dir=$(cat "$RESULTS_DIR/.last_result_dir" 2>/dev/null || true)
    if [ -z "$dir" ] || [ ! -d "$dir" ]; then
        log "FAIL ${case_id}: no result directory"
        return 1
    fi
    append_manifest "$case_id" "$name" "$expected" "$dir"
    printf '\n----- %s 自动判定 -----\n' "$case_id"
    grep -E '^(case|PASS|aisbench|agent-status posts|motor kvc delta|engine edits|engine metrics|reasons):' \
        "$dir/summary.txt" || true
    printf '\n'
}

run_all() {
    require_model
    new_run_dir
    log "mode=$MODE qa_dir=$QA_DIR"
    bash "$KIT_DIR/scripts/build_datasets.sh" $([ "$FAST" = "1" ] && echo --fast)

    ensure_stack off 0 0 off
    run_one S1 "压满基线（KVC off）" \
        "KV usage max>=70% 或 Waiting>0" "$MODEL" kvc_manual_pause_offload_multi "$S1_DURATION"

    ensure_stack on 0 0 off
    run_one S2 "finalize -> evict" \
        "evict 与 finalize 数匹配; offload/prefetch=0; edit_fail=0" \
        "$MODEL" "$DATASET_FIN"

    ensure_stack on "$IDLE_OFFLOAD" 0 on
    run_one S3 "pause -> offload -> prefetch" \
        "offload>0; prefetch>0 且 <=offload; edit_fail=0" \
        "$MODEL" "$DATASET_FUNC"

    ensure_stack on "$IDLE_OFFLOAD" 0.7 on
    run_one S5 "压满触发（high_water=0.7）" \
        "KV usage max>=70%; offload>0; edit_fail=0" \
        "$MODEL" "$DATASET_SAT_PAUSE" "$S5_DURATION"

    ensure_stack off 0 0 off
    run_one S6 "KVC off 对照" \
        "status 200; KVC 计数=0; engine edit=0" \
        "$MODEL_GATE" "$DATASET_GATE"

    ensure_stack on "$IDLE_OFFLOAD" 1.0 on
    run_one S7 "high-water gate" \
        "gate_skip>0; offload=0; evict=0" \
        "$MODEL_GATE" "$DATASET_GATE"

    log "generating report"
    python3 "$KIT_DIR/scripts/make_qa_report.py" --run-dir "$QA_DIR"
    printf '\n汇总报告：%s/QA_REPORT.md\n' "$QA_DIR"
    printf 'HTML 报告：%s/QA_REPORT.html\n' "$QA_DIR"
    printf '最新副本：%s/QA_REPORT_latest.md\n' "$RESULTS_DIR"

    if [ "${KEEP_STACK:-0}" != "1" ]; then
        bash "$KIT_DIR/scripts/stop_stack.sh"
    fi
}

init_cmd() {
    local target="$KIT_DIR/kvc_test.env"
    if [ -f "$target" ]; then
        log "config already exists: $target"
        return 0
    fi
    cp "$KIT_DIR/kvc_test.env.example" "$target"
    log "created $target"
    if [ -t 0 ]; then
        set_env() {
            python3 - "$1" "$2" "$3" <<'PYENV'
import sys
path, key, value = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines()
out, found = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PYENV
        }
        local value
        read -r -p "BENCH_SOURCE_DIR (benchmark repo path): " value; [ -n "$value" ] && set_env "$target" BENCH_SOURCE_DIR "$value"
        read -r -p "MODEL_PATH: " value; [ -n "$value" ] && set_env "$target" MODEL_PATH "$value"
        read -r -p "MODEL_NAME [my-model]: " value; [ -n "$value" ] && set_env "$target" MODEL_NAME "$value"
        read -r -p "CARD [0]: " value; [ -n "$value" ] && set_env "$target" CARD "$value"
        read -r -p "ENGINE_IMAGE [$ENGINE_IMAGE]: " value; [ -n "$value" ] && set_env "$target" ENGINE_IMAGE "$value"
        read -r -p "MOTOR_IMAGE [$MOTOR_IMAGE]: " value; [ -n "$value" ] && set_env "$target" MOTOR_IMAGE "$value"
        read -r -p "AIS_IMAGE [$AIS_IMAGE]: " value; [ -n "$value" ] && set_env "$target" AIS_IMAGE "$value"
        read -r -p "WORK_DIR [$WORK_DIR]: " value; [ -n "$value" ] && set_env "$target" WORK_DIR "$value"
    fi
    log "edit $target, then run: $0 doctor"
}

selftest_cmd() {
    local fails=0
    for f in "$KIT_DIR"/scripts/*.sh "$KIT_DIR/qa_run.sh"; do
        bash -n "$f" || fails=1
    done
    python3 -m py_compile "$KIT_DIR"/scripts/*.py || fails=1
    local p1="/home/t0088"
    local p2="7700"
    local p3="/home/"
    local p4="RAG_data"
    local p5="kv-"
    local p6="hint"
    local p7="agent"
    local p8="x"
    if grep -RInE "${p1}${p2}|${p3}${p4}|${p5}${p6}|${p7}${p8}" \
        "$KIT_DIR/scripts" "$KIT_DIR/qa_run.sh" 2>/dev/null; then
        log "FAIL hardcoded developer paths found in runtime scripts"
        fails=1
    fi
    python3 "$KIT_DIR/scripts/render_ais_configs.py" >/dev/null || fails=1
    if [ "$fails" -eq 0 ]; then
        log "selftest PASS"
    else
        log "selftest FAIL"
    fi
    return "$fails"
}

prepare_motor_cmd() {
    [ "$MOTOR_INSTALL_MODE" = "source" ] || {
        log "MOTOR_INSTALL_MODE=$MOTOR_INSTALL_MODE; nothing to build"
        return 0
    }
    [ -n "$MOTOR_SOURCE_DIR" ] && [ -d "$MOTOR_SOURCE_DIR" ] \
      || die "MOTOR_SOURCE_DIR is not configured or does not exist"
    log "building Motor wheel in $MOTOR_SOURCE_DIR"
    (cd "$MOTOR_SOURCE_DIR" && bash build.sh)
    ls -t "$MOTOR_SOURCE_DIR"/dist/motor-*.whl | head -1
}

bundle_cmd() {
    bash "$KIT_DIR/scripts/bundle.sh"
}

ensure_datasets() {
    local probe
    if [ "$FAST" = "1" ]; then
        probe="$DATA_DIR/kvc_manual_pause_offload_functional_fast.jsonl"
    else
        probe="$DATA_DIR/kvc_manual_pause_offload_functional.jsonl"
    fi
    if [ ! -f "$probe" ]; then
        bash "$KIT_DIR/scripts/build_datasets.sh" $([ "$FAST" = "1" ] && echo --fast)
    fi
}

run_case_cmd() {
    local case_id="${1:-}"
    require_model
    ensure_datasets
    case "$case_id" in
        s1|S1)
            new_run_dir
            ensure_stack off 0 0 off
            run_one S1 "压满基线（KVC off）" "KV usage max>=70% 或 Waiting>0" \
                "$MODEL" kvc_manual_pause_offload_multi "$S1_DURATION"
            ;;
        s2|S2)
            new_run_dir
            ensure_stack on 0 0 off
            run_one S2 "finalize -> evict" "evict 与 finalize 数匹配; edit_fail=0" \
                "$MODEL" "$DATASET_FIN"
            ;;
        s3|S3)
            new_run_dir
            ensure_stack on "$IDLE_OFFLOAD" 0 on
            run_one S3 "pause -> offload -> prefetch" "offload>0; prefetch>0; edit_fail=0" \
                "$MODEL" "$DATASET_FUNC"
            ;;
        s5|S5)
            new_run_dir
            ensure_stack on "$IDLE_OFFLOAD" 0.7 on
            run_one S5 "压满触发（high_water=0.7）" "KV max>=70%; offload>0" \
                "$MODEL" "$DATASET_SAT_PAUSE" "$S5_DURATION"
            ;;
        s6|S6)
            new_run_dir
            ensure_stack off 0 0 off
            run_one S6 "KVC off 对照" "status 200; KVC 计数=0; engine edit=0" \
                "$MODEL_GATE" "$DATASET_GATE"
            ;;
        s7|S7)
            new_run_dir
            ensure_stack on "$IDLE_OFFLOAD" 1.0 on
            run_one S7 "high-water gate" "gate_skip>0; offload=0; evict=0" \
                "$MODEL_GATE" "$DATASET_GATE"
            ;;
        *)
            echo "unknown case: $case_id (use s1 s2 s3 s5 s6 s7)" >&2
            exit 2
            ;;
    esac
    python3 "$KIT_DIR/scripts/make_qa_report.py" --run-dir "$QA_DIR"
    printf '\n报告：%s/QA_REPORT.md\n' "$QA_DIR"
    if [ "${KEEP_STACK:-0}" != "1" ]; then
        bash "$KIT_DIR/scripts/stop_stack.sh"
    fi
}

report_cmd() {
    local latest
    latest=$(ls -1dt "$RESULTS_DIR"/*_qa_* 2>/dev/null | head -1 || true)
    [ -n "$latest" ] || die "no qa run found under $RESULTS_DIR"
    python3 "$KIT_DIR/scripts/make_qa_report.py" --run-dir "$latest"
    printf 'report: %s/QA_REPORT.md\n' "$latest"
}

case "$CMD" in
    init)     init_cmd ;;
    doctor)   bash "$KIT_DIR/scripts/preflight.sh" ;;
    selftest) selftest_cmd ;;
    prepare-motor) prepare_motor_cmd ;;
    bundle)   bundle_cmd ;;
    datasets) bash "$KIT_DIR/scripts/build_datasets.sh" $([ "$FAST" = "1" ] && echo --fast) ;;
    all)      run_all ;;
    case)     run_case_cmd "${1:-}" ;;
    observe)  exec python3 "$KIT_DIR/scripts/observe.py" ;;
    report)   report_cmd ;;
    stop)     bash "$KIT_DIR/scripts/stop_stack.sh" ;;
    help|-h|--help) usage ;;
    *) usage; exit 2 ;;
esac
