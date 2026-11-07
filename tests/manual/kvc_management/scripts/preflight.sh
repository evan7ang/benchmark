#!/usr/bin/env bash
# Environment preflight / doctor for the KVC manual test kit.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

FAIL=0
WARN=0
ok()   { log "OK   $*"; }
warn() { log "WARN $*"; WARN=$((WARN + 1)); }
bad()  { log "FAIL $*"; FAIL=$((FAIL + 1)); }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 && ok "command: $1" || bad "command missing: $1"
}

check_file() {
    [ -e "$1" ] && ok "file: $1" || bad "file missing: $1"
}

check_image() {
    "$DOCKER_BIN" image inspect "$1" >/dev/null 2>&1 && ok "image: $1" || bad "image missing: $1"
}

port_free() {
    local port="$1" name="$2"
    if python3 - "$port" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
    then
        ok "port free: $name :$port"
    else
        warn "port already in use: $name :$port"
    fi
}

log "===== KVC manual test doctor ====="
log "config: ${KVC_TEST_ENV_FILE:-<built-in defaults>}"
log "work_dir=$WORK_DIR data_dir=$DATA_DIR results_dir=$RESULTS_DIR"

need_cmd "$DOCKER_BIN"
need_cmd curl
need_cmd python3
need_cmd bash
command -v npu-smi >/dev/null 2>&1 && ok "command: npu-smi" || warn "npu-smi not found (device checks degraded)"
python3 - <<'PY' || bad "python3 >= 3.9 is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

if [ -z "$KVC_TEST_ENV_FILE" ]; then
    warn "no kvc_test.env found; run ./qa_run.sh init and edit it before testing"
fi

[ -n "$MODEL_PATH" ] && check_file "$MODEL_PATH" || bad "MODEL_PATH is not configured"
[ -n "$TOKENIZER_PATH" ] && check_file "$TOKENIZER_PATH" || bad "TOKENIZER_PATH is not configured"
[ -f "$MODEL_PATH/config.json" ] && ok "model config.json" || warn "model config.json not found under MODEL_PATH"

[ -d "$BENCH_DIR/ais_bench" ] && ok "benchmark source: $BENCH_DIR" || bad "benchmark source not found: $BENCH_DIR (set BENCH_SOURCE_DIR)"

check_image "$ENGINE_IMAGE"
check_image "$MOTOR_IMAGE"
check_image "$AIS_IMAGE"

case "$MOTOR_INSTALL_MODE" in
    image)
        "$DOCKER_BIN" run --rm --entrypoint bash "$MOTOR_IMAGE" -lc 'python3 -c "import motor"' >/dev/null 2>&1 \
          && ok "Motor image contains motor package" \
          || bad "Motor image does not contain the motor package: $MOTOR_IMAGE"
        ;;
    wheel)
        check_file "$MOTOR_WHEEL"
        ;;
    source)
        [ -d "$MOTOR_SOURCE_DIR" ] && ok "MOTOR_SOURCE_DIR: $MOTOR_SOURCE_DIR" || bad "MOTOR_SOURCE_DIR missing"
        ;;
    *) bad "unknown MOTOR_INSTALL_MODE=$MOTOR_INSTALL_MODE" ;;
esac

if [ "$AIS_RUN_MODE" = "host" ]; then
    command -v ais_bench >/dev/null 2>&1 && ok "host ais_bench" || bad "host ais_bench not found"
    PYTHONPATH="$BENCH_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY' \
      && ok "AISBench source has delay_after_ms support" \
      || bad "AISBench source is missing delay_after_ms support"
from ais_bench.benchmark.openicl.icl_inferencer.icl_base_api_inferencer import BaseApiInferencer
assert hasattr(BaseApiInferencer, "_delay_after_ms")
PY
else
    "$DOCKER_BIN" run --rm --entrypoint bash \
      -v "$BENCH_DIR:/workspace" -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      "$AIS_IMAGE" -lc 'python3 -c "from ais_bench.benchmark.openicl.icl_inferencer.icl_base_api_inferencer import BaseApiInferencer; assert hasattr(BaseApiInferencer, \"_delay_after_ms\")"' \
      >/dev/null 2>&1 && ok "AISBench image + source has delay_after_ms support" \
      || bad "AISBench image/source is missing delay_after_ms support"
fi

log "----- ports -----"
port_free "$ENGINE_PORT" engine
port_free "$MOTOR_INFER_PORT" motor-infer
port_free "$MOTOR_MGMT_PORT" motor-mgmt
port_free "$MOTOR_OBS_PORT" motor-obs
port_free "$MMC_META_PORT" memcache-meta
port_free "$MMC_STORE_PORT" memcache-store
port_free "$MMC_METRICS_PORT" memcache-metrics
port_free "$MMC_LOOKUP_PORT" memcache-lookup

log "----- devices -----"
IFS=',' read -r -a cards <<< "$CARDS"
for card in "${cards[@]}"; do
    [ -e "/dev/davinci${card}" ] && ok "/dev/davinci${card}" || bad "/dev/davinci${card} missing"
done
for dev in /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc; do
    [ -e "$dev" ] && ok "$dev" || warn "$dev missing"
done
for path in "$ASCEND_DRIVER_PATH" "$ASCEND_INSTALL_INFO" "$ASCEND_DCMI"; do
    [ -e "$path" ] && ok "Ascend path: $path" || warn "Ascend path missing: $path"
done

log "----- NPU free memory -----"
if command -v npu-smi >/dev/null 2>&1; then
    NPU_INFO=$(mktemp)
    npu-smi info > "$NPU_INFO" 2>/dev/null || true
    python3 - "$CARDS" "$GPU_MEM_UTIL" "$NPU_INFO" <<'PYNPU' || true
import re, sys
cards = set(x.strip() for x in sys.argv[1].split(",") if x.strip())
gmu = float(sys.argv[2])
current = None
for line in open(sys.argv[3], encoding="utf-8", errors="replace"):
    m = re.match(r"\|\s*(\d+)\s+910", line)
    if m:
        current = m.group(1)
    nums = re.findall(r"(\d+)\s*/\s*(\d+)", line)
    if current in cards and nums:
        used, total = map(int, nums[-1])
        if total <= 0:
            continue
        ratio = used / total
        free = total - used
        if ratio >= 0.5:
            print(f"FAIL NPU {current}: used={used}MB total={total}MB free={free}MB ({ratio*100:.1f}% used)")
            print(f"     free HBM is likely too low for GPU_MEM_UTIL={gmu}; choose another CARD")
        elif ratio >= 0.1:
            print(f"WARN NPU {current}: used={used}MB total={total}MB free={free}MB ({ratio*100:.1f}% used)")
        else:
            print(f"OK   NPU {current}: used={used}MB total={total}MB free={free}MB ({ratio*100:.1f}% used)")
PYNPU
    rm -f "$NPU_INFO"
fi

log "----- data mode: $KVC_DATA_MODE -----"
case "$KVC_DATA_MODE" in
    synthetic)
        "$DOCKER_BIN" run --rm --entrypoint bash \
          -v "$BENCH_DIR:/workspace" -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
          -v "$MODEL_PATH:$MODEL_PATH:ro" "$AIS_IMAGE" \
          -lc 'python3 -c "import transformers; from transformers import AutoTokenizer"' >/dev/null 2>&1 \
          && ok "AIS image has transformers for synthetic data" \
          || bad "AIS image is missing transformers (required for synthetic data)"
        ok "synthetic datasets will be generated into $DATA_DIR"
        ;;
    legacy)
        check_file "$SOURCE_DATA_DIR/swebench_full50_v2_pause_offload.jsonl"
        check_file "$SOURCE_DATA_DIR/swebench_full50_v2_finalize.jsonl"
        ;;
    external)
        for name in kvc_manual_pause_offload_functional kvc_manual_finalize_quick \
                    kvc_manual_gate_quick kvc_manual_pause_offload_quick; do
            [ -f "$DATA_DIR/${name}.jsonl" ] && ok "dataset: $name" || warn "external dataset missing: $name"
        done
        ;;
    *) bad "unknown KVC_DATA_MODE=$KVC_DATA_MODE" ;;
esac

# Record the exact artifacts used by this environment.
BENCH_COMMIT=$(git -C "$BENCH_DIR" rev-parse HEAD 2>/dev/null || echo "")
ENGINE_ID=$("$DOCKER_BIN" image inspect -f '{{.Id}}' "$ENGINE_IMAGE" 2>/dev/null || echo "")
MOTOR_ID=$("$DOCKER_BIN" image inspect -f '{{.Id}}' "$MOTOR_IMAGE" 2>/dev/null || echo "")
AIS_ID=$("$DOCKER_BIN" image inspect -f '{{.Id}}' "$AIS_IMAGE" 2>/dev/null || echo "")
python3 - "$WORK_DIR/MANIFEST.json" "$BENCH_COMMIT" "$ENGINE_IMAGE" "$ENGINE_ID" \
    "$MOTOR_IMAGE" "$MOTOR_ID" "$AIS_IMAGE" "$AIS_ID" "$MODEL_PATH" "$MODEL_NAME" \
    "$MAX_MODEL_LEN" "$CHAT_API" "$KVC_DATA_MODE" "$MOTOR_INSTALL_MODE" "$KVC_TEST_ENV_FILE" <<'PYMANIFEST'
import json, sys
(path, bench_commit, engine_image, engine_id, motor_image, motor_id,
 ais_image, ais_id, model_path, model_name, max_model_len, chat_api,
 data_mode, motor_mode, env_file) = sys.argv[1:]
json.dump({
    "benchmark_commit": bench_commit,
    "engine_image": {"name": engine_image, "id": engine_id},
    "motor_image": {"name": motor_image, "id": motor_id},
    "ais_image": {"name": ais_image, "id": ais_id},
    "model": {"path": model_path, "name": model_name,
              "max_model_len": int(max_model_len), "chat_api": chat_api},
    "data_mode": data_mode,
    "motor_install_mode": motor_mode,
    "config_file": env_file,
}, open(path, "w"), indent=2)
PYMANIFEST

log "===== doctor summary: failures=$FAIL warnings=$WARN ====="
if [ "$FAIL" -ne 0 ]; then
    die "doctor found $FAIL blocking issue(s)"
fi
log "doctor passed"
