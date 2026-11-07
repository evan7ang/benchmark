#!/usr/bin/env bash
# Build manual KVC datasets and run tokenizer-level context validation.
#
#   build_datasets.sh          # standard: idle=30s, delay=45s
#   build_datasets.sh --fast   # fast: idle=10s, delay=15s (only affects legacy)
#
# KVC_DATA_MODE:
#   synthetic (default): generate deterministic prompts with the model tokenizer
#   legacy             : use SWE-bench-derived JSONL from SOURCE_DATA_DIR
#   external           : use pre-built JSONL already present in DATA_DIR
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

MODE="standard"
[ "${1:-}" = "--fast" ] && MODE="fast"
mkdir -p "$DATA_DIR"

filter_one() {
    local name="$1"
    local file="$DATA_DIR/${name}.jsonl"
    [ -f "$file" ] || { log "WARN missing $file"; return 0; }
    log "context-validating $name"
    docker run --rm --entrypoint bash \
      -v "$BENCH_DIR:/workspace" \
      -v "$KIT_DIR:/kvc_kit:ro" \
      -v "$DATA_DIR:$DATA_DIR" \
      -v "$MODEL_PATH:$MODEL_PATH:ro" \
      -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      "$AIS_IMAGE" -lc "python3 /kvc_kit/scripts/filter_dataset_context.py \
        --input '$file' --output '${file}.filtered' --tokenizer '$TOKENIZER_PATH' --max-context '$MAX_MODEL_LEN'" \
      2>&1 | grep -vE 'UserWarning|from scipy|npu-smi' || true
    if [ -f "${file}.filtered" ]; then
        mv "$file" "${file}.raw"
        mv "${file}.filtered" "$file"
        mv "${file}.filtered.report.json" "${file}.report.json"
    fi
}

build_synthetic() {
    log "generating synthetic datasets with tokenizer: $TOKENIZER_PATH"
    docker run --rm --entrypoint bash \
      -v "$BENCH_DIR:/workspace" \
      -v "$KIT_DIR:/kvc_kit:ro" \
      -v "$DATA_DIR:$DATA_DIR" \
      -v "$MODEL_PATH:$MODEL_PATH:ro" \
      -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      -e MODEL_PATH -e TOKENIZER_PATH -e MAX_MODEL_LEN -e CHAT_API -e DATA_DIR \
      -e FAST_IDLE_OFFLOAD -e FAST_DELAY_MS -e STD_IDLE_OFFLOAD -e STD_DELAY_MS \
      "$AIS_IMAGE" -lc "python3 /kvc_kit/scripts/make_synthetic_datasets.py --scale all --out '$DATA_DIR'" 2>&1 \
      | grep -vE 'UserWarning|from scipy|npu-smi' || true
}

build_legacy() {
    log "building legacy datasets from SOURCE_DATA_DIR=$SOURCE_DATA_DIR"
    [ -n "$SOURCE_DATA_DIR" ] || die "SOURCE_DATA_DIR is empty (required for KVC_DATA_MODE=legacy)"
    local pause_src="$SOURCE_DATA_DIR/swebench_full50_v2_pause_offload.jsonl"
    local finalize_src="$SOURCE_DATA_DIR/swebench_full50_v2_finalize.jsonl"
    [ -f "$pause_src" ] || die "missing legacy source: $pause_src"
    [ -f "$finalize_src" ] || die "missing legacy source: $finalize_src"

    if [ ! -f "$DATA_DIR/kvc_manual_pause_offload_quick.jsonl" ]; then
        python3 "$KIT_DIR/scripts/build_quick_dataset.py" \
            --mode pause --input-pause "$pause_src" --input-finalize "$finalize_src" \
            --out-dir "$DATA_DIR" --delay-ms "$STD_DELAY_MS"
        filter_one kvc_manual_pause_offload_quick
    fi
    if [ "$MODE" = "fast" ]; then
        python3 "$KIT_DIR/scripts/build_quick_dataset.py" \
            --mode pause-functional --tag _fast --input-pause "$pause_src" --input-finalize "$finalize_src" \
            --out-dir "$DATA_DIR" --delay-ms "$FAST_DELAY_MS" --max-out-cap 256 \
            --pause-functional-sessions 3 --pause-functional-rows 15 --pause-functional-pauses 3
        python3 "$KIT_DIR/scripts/build_quick_dataset.py" \
            --mode finalize --tag _fast --input-pause "$pause_src" --input-finalize "$finalize_src" \
            --out-dir "$DATA_DIR" --delay-ms "$FAST_DELAY_MS" --max-out-cap 256 \
            --finalize-target-sessions 3 --finalize-target-rows 15
        python3 "$KIT_DIR/scripts/build_quick_dataset.py" \
            --mode gate --tag _fast --input-pause "$pause_src" --input-finalize "$finalize_src" \
            --out-dir "$DATA_DIR" --delay-ms "$FAST_DELAY_MS" --gate-max-out-cap 64
        python3 "$KIT_DIR/scripts/build_saturation_pause.py" \
            --input "$DATA_DIR/kvc_manual_pause_offload_quick.jsonl" \
            --output "$DATA_DIR/kvc_manual_saturation_pause_fast.jsonl" \
            --delay-ms "$FAST_DELAY_MS"
        for name in kvc_manual_pause_offload_functional_fast kvc_manual_finalize_quick_fast \
                    kvc_manual_gate_quick_fast kvc_manual_saturation_pause_fast; do
            filter_one "$name"
        done
    else
        python3 "$KIT_DIR/scripts/build_quick_dataset.py" \
            --mode all --input-pause "$pause_src" --input-finalize "$finalize_src" \
            --out-dir "$DATA_DIR" --delay-ms "$STD_DELAY_MS"
        python3 "$KIT_DIR/scripts/build_saturation_pause.py" \
            --input "$DATA_DIR/kvc_manual_pause_offload_quick.jsonl" \
            --output "$DATA_DIR/kvc_manual_saturation_pause.jsonl" \
            --delay-ms "$STD_DELAY_MS"
        for name in kvc_manual_pause_offload_functional kvc_manual_finalize_quick \
                    kvc_manual_gate_quick kvc_manual_saturation_pause; do
            filter_one "$name"
        done
    fi
}

build_external() {
    log "using external datasets in DATA_DIR=$DATA_DIR"
    for name in kvc_manual_pause_offload_functional kvc_manual_pause_offload_functional_fast \
                kvc_manual_finalize_quick kvc_manual_finalize_quick_fast \
                kvc_manual_gate_quick kvc_manual_gate_quick_fast \
                kvc_manual_pause_offload_quick kvc_manual_saturation_pause kvc_manual_saturation_pause_fast; do
        [ -f "$DATA_DIR/${name}.jsonl" ] || log "WARN external dataset missing: $DATA_DIR/${name}.jsonl"
    done
}

case "$KVC_DATA_MODE" in
    synthetic) build_synthetic ;;
    legacy) build_legacy ;;
    external) build_external ;;
    *) die "unknown KVC_DATA_MODE=$KVC_DATA_MODE (synthetic|legacy|external)" ;;
esac

# Always render the AISBench configs after data generation.
python3 "$KIT_DIR/scripts/render_ais_configs.py" >/dev/null

log "datasets ready in $DATA_DIR (mode=$MODE, source=$KVC_DATA_MODE)"
ls -1 "$DATA_DIR"/*.jsonl 2>/dev/null | grep -v '\.raw' | tail -20 || true
