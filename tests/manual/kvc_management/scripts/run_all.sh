#!/usr/bin/env bash
# Run all KVC manual scenarios sequentially. Takes roughly 60-90 minutes.
#
# Each KVC-on scenario restarts Motor because finalize is terminal and session
# ids are reused across datasets.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

run_case() {
    bash "$KIT_DIR/scripts/run_case.sh" "$@"
}

log "===== S1 saturation ramp (KVC off) ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc off --card "$CARD" --port "$ENGINE_PORT"
run_case --case ramp_kvc_off_pause_c8 --model kvc_manual_c8 \
    --dataset kvc_manual_pause_offload_multi --duration 180

log "===== S2 finalize -> evict ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc on --idle-offload 0 --high-water 0 \
    --prefetch off --no-engine --no-meta
run_case --case finalize_evict --model kvc_manual_c8 \
    --dataset kvc_manual_finalize_quick

log "===== S3/S4 pause -> offload -> prefetch ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc on --idle-offload 30 --high-water 0 \
    --prefetch on --no-engine --no-meta
run_case --case pause_offload_prefetch --model kvc_manual_c8 \
    --dataset kvc_manual_pause_offload_quick

log "===== S5 saturated trigger (high_water=0.7) ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc on --idle-offload 30 --high-water 0.7 \
    --prefetch on --no-engine --no-meta
run_case --case saturated --model kvc_manual_c8 \
    --dataset kvc_manual_saturation_pause --duration 300

log "===== S6 KVC off control ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc off --no-engine --no-meta
run_case --case kvc_off --model kvc_manual_c1 --dataset kvc_manual_gate_quick

log "===== S7 high-water gate ====="
bash "$KIT_DIR/scripts/start_stack.sh" --kvc on --idle-offload 30 --high-water 1.0 \
    --prefetch on --no-engine --no-meta
run_case --case gate --model kvc_manual_c1 --dataset kvc_manual_gate_quick

log "===== all scenarios finished ====="
ls -1dt "$RESULTS_DIR"/* | head -10

if [ "${KEEP_STACK:-0}" != "1" ]; then
    bash "$KIT_DIR/scripts/stop_stack.sh"
fi
