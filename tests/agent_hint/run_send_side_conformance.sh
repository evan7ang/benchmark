#!/usr/bin/env bash
# Send-side conformance without a GPU.
#
# Starts the mock OpenAI server, runs AISBench ON/OFF cases for the existing
# datasets, and checks the exact X-Agent-Hint/status events against the source
# JSONL. Requires Docker and the ais-runner image; no NPU is used.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${AGENT_HINT_DATA_DIR:-/home/t00887700/benchmark-data/agent_replay_v2}"
MOCK_PORT="${AGENT_HINT_MOCK_PORT:-18080}"
AIS_IMAGE="${AIS_IMAGE:-ais-runner:ais-1}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BASE="${AGENT_HINT_CONFORMANCE_DIR:-$ROOT/outputs/agent-hint-ci/conformance-$STAMP}"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

mkdir -p "$BASE"
REL_BASE="${BASE#"$ROOT"/}"
CONTAINER_BASE="/workspace/$REL_BASE"

if ! docker image inspect "$AIS_IMAGE" >/dev/null 2>&1; then
  echo "missing Docker image: $AIS_IMAGE" >&2
  exit 2
fi
for name in example_v2 swebench_smoke_v2_finalize swebench_full50_v2_running swebench_full50_v2_finalize; do
  case "$name" in
    example_v2) path="$ROOT/ais_bench/datasets/agent/example_v2.jsonl" ;;
    *) path="$DATA_DIR/$name.jsonl" ;;
  esac
  if [ ! -f "$path" ]; then
    echo "missing dataset: $path" >&2
    exit 2
  fi
done

start_mock() {
  local record_path="$1"
  docker rm -f agent-hint-mock >/dev/null 2>&1 || true
  docker run -d --name agent-hint-mock --network host --entrypoint bash \
    -v "$ROOT":/workspace \
    -v "$DATA_DIR":"$DATA_DIR":ro \
    -w /workspace \
    -e MOCK_PORT="$MOCK_PORT" \
    -e MOCK_RECORD_PATH="$record_path" \
    "$AIS_IMAGE" \
    -lc 'python3 /workspace/ais_bench/datasets/agent/mock_agent_hint_server.py' >/dev/null
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${MOCK_PORT}/v1/models" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "mock failed to start" >&2
  docker logs --tail 40 agent-hint-mock >&2
  return 1
}

run_aisbench() {
  local case_dir="$1" extra_env="$2" cmd="$3"
  docker run --rm --network host --entrypoint bash \
    -v "$ROOT":/workspace \
    -v "$DATA_DIR":"$DATA_DIR":ro \
    -v /home/RAG_data/models:/home/RAG_data/models:ro \
    -w /workspace \
    -e PYTHONPATH=/workspace \
    -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
    -e AISBENCH_HOST_IP=127.0.0.1 \
    -e AISBENCH_HOST_PORT="$MOCK_PORT" \
    -e AGENT_HINT_DATA_DIR="$DATA_DIR" \
    -e AISBENCH_AGENT_HINT_AUDIT_PATH="${case_dir}/audit.jsonl" \
    ${extra_env:+$extra_env} \
    "$AIS_IMAGE" \
    -lc "pip uninstall -y av >/tmp/pip-av.log 2>&1; ${cmd}"
}

run_check() {
  local case_dir="$1" dataset="$2" expect="$3" log_path="$4" received="$5"
  docker run --rm --network host --entrypoint bash \
    -v "$ROOT":/workspace \
    -v "$DATA_DIR":"$DATA_DIR":ro \
    -w /workspace \
    -e PYTHONPATH=/workspace \
    -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
    "$AIS_IMAGE" \
    -lc "python3 /workspace/ais_bench/datasets/agent/check_send_side.py \
      --dataset '$dataset' \
      --audit '${case_dir}/audit.jsonl' \
      --received '${case_dir}/received.jsonl' \
      --expect $expect \
      --log '$log_path'"
}

run_case() {
  local name="$1" model="$2" dataset_cfg="$3" dataset_path="$4" expect="$5"
  local case_dir="$CONTAINER_BASE/$name"
  local host_case="$BASE/$name"
  rm -rf "$host_case"
  mkdir -p "$host_case"
  echo
  echo "==================== $name (expect=$expect) ===================="
  start_mock "${case_dir}/received.jsonl"
  run_aisbench "$case_dir" "" \
    "ais_bench --models $model --datasets $dataset_cfg --mode perf --num-warmups 0 --work-dir ${case_dir}/run >/tmp/${name}.log 2>&1; rc=\$?; echo AISBench exit=\$rc; tail -2 /tmp/${name}.log; exit \$rc"
  local host_out
  host_out=$(find "$host_case/run" -path '*/logs/infer/*/*.out' | head -1 || true)
  local log_arg=""
  if [ -n "$host_out" ]; then
    log_arg="${host_out/"$ROOT"/\/workspace}"
  fi
  run_check "$case_dir" "$dataset_path" "$expect" "$log_arg" "${case_dir}/received.jsonl"
}

# A synthetic timestamp pacing case: session-aware replay must honor
# timestamps (via TokenProducer) even though the requests stay in one session.
run_timestamp_case() {
  local case_dir="$CONTAINER_BASE/timestamp"
  local host_case="$BASE/timestamp"
  rm -rf "$host_case"
  mkdir -p "$host_case/configs/models/vllm_api" "$host_case/configs/datasets/custom"
  cat > "$host_case/ts.jsonl" <<'EOF'
{"question":"first","max_out_len":1,"timestamp":0,"agent_hint":{"session":{"session_id":"ts-1"},"agent_status":{"state":"running"}}}
{"question":"second","max_out_len":1,"timestamp":6000,"agent_hint":{"session":{"session_id":"ts-1"},"agent_status":{"state":"running"}}}
EOF
  cat > "$host_case/configs/datasets/custom/ts_probe.py" <<EOF
from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset
reader_cfg = dict(input_columns=["question", "max_out_len", "timestamp"], output_column=None)
infer_cfg = dict(prompt_template=dict(type=PromptTemplate, template="{question}"), retriever=dict(type=ZeroRetriever), inferencer=dict(type=GenInferencer))
eval_cfg = dict(evaluator=None, pred_role="BOT")
ts_probe_datasets = [dict(abbr="ts-probe", type=CustomDataset, path="$case_dir/ts.jsonl", reader_cfg=reader_cfg, infer_cfg=infer_cfg, eval_cfg=eval_cfg)]
EOF
  cat > "$host_case/configs/models/vllm_api/ts_probe_on.py" <<'EOF'
from ais_bench.benchmark.models import VLLMCustomAPI
models = [dict(attr="service", type=VLLMCustomAPI, abbr="ts-probe-on", path="/home/RAG_data/models/Qwen3-8B", model="kvh", stream=True, use_chat_api=True, session_aware=True, session_concurrency=1, agent_hint_transport="header", pass_agent_hint=True, agent_status_report=True, extra_headers={}, json_extra_fields=["messages", "tools"], use_timestamp=True, batch_size=1, request_rate=0, retry=2, api_key="", host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1"), host_port=int(__import__("os").environ.get("AISBENCH_HOST_PORT", "18080")), url="", max_out_len=1, generation_kwargs=dict(temperature=0, ignore_eos=True))]
EOF
  echo
  echo "==================== timestamp-pacing ===================="
  start_mock "$case_dir/received.jsonl"
  run_aisbench "$case_dir" "" \
    "ais_bench --config-dir ${case_dir}/configs --models ts_probe_on --datasets ts_probe --mode perf --num-warmups 0 --work-dir ${case_dir}/run >/tmp/timestamp.log 2>&1; rc=\$?; echo AISBench exit=\$rc; tail -2 /tmp/timestamp.log; exit \$rc"
  python3 - "$host_case/received.jsonl" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(line) for line in open(path, encoding="utf-8") if '"received_request"' in line]
if len(rows) != 2:
    raise SystemExit(f"timestamp-pacing: expected 2 requests, got {len(rows)}")
delta = rows[1]["ts"] - rows[0]["ts"]
print(f"timestamp-pacing delta={delta:.3f}s")
if delta < 5.0:
    raise SystemExit(f"timestamp-pacing failed: delta {delta:.3f}s < 5.0s")
PY
}

run_pause_event_case() {
  local case_dir="$CONTAINER_BASE/pause-event"
  local host_case="$BASE/pause-event"
  rm -rf "$host_case"
  mkdir -p "$host_case/model_io" "$host_case/configs/models/vllm_api" "$host_case/configs/datasets/custom"
  python3 - "$host_case" <<'PY'
import json, sys
base = sys.argv[1]
for index, start in enumerate((1000, 5000, 9000, 13000), 1):
    record = {
        "schema_version": "harness-capture-v1",
        "case_id": "pause-case",
        "ordinal": index,
        "global_request_ordinal": index,
        "request_kind": "inference",
        "session_id": "s-pause",
        "timing": {"source_utc": {"start_time": start}},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "request": {
            "messages": [{"role": "user", "content": f"turn {index}"}],
            "tools": [],
            "agent_hint": {"session": {"session_id": "s-pause"}, "agent_status": {"state": "running"}},
        },
    }
    with open(f"{base}/model_io/capture_{index:06d}.json", "w", encoding="utf-8") as handle:
        json.dump(record, handle)
events = [
    {"session": {"session_id": "s-pause"}, "agent_status": {"state": "pause", "state_since_ms": 2000}},
    {"session": {"session_id": "s-pause"}, "agent_status": {"state": "running", "state_since_ms": 2100}},
    {"session": {"session_id": "s-pause"}, "agent_status": {"state": "pause", "state_since_ms": 6000}},
    {"session": {"session_id": "s-pause"}, "agent_status": {"state": "running", "state_since_ms": 6100}},
    {"session": {"session_id": "s-pause"}, "agent_status": {"state": "finalize", "state_since_ms": 14000}},
]
with open(f"{base}/status.jsonl", "w", encoding="utf-8") as handle:
    for event in events:
        handle.write(json.dumps(event) + "\n")
PY
  echo
  echo "==================== pause-event-replay ===================="
  run_aisbench "$case_dir" "" \
    "python3 -m ais_bench.capture build --input-dir ${case_dir}/model_io --output ${case_dir}/dataset_v2.jsonl --builder capture --state-mode completed-finalize --status-events ${case_dir}/status.jsonl --replay-pause-events --skip-tokenizer-check >/tmp/pause-build.log 2>&1; rc=\$?; echo build exit=\$rc; tail -3 /tmp/pause-build.log; exit \$rc"
  cat > "$host_case/configs/datasets/custom/pause_probe.py" <<EOF
from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset
reader_cfg = dict(input_columns=["question", "max_out_len", "timestamp"], output_column=None)
infer_cfg = dict(prompt_template=dict(type=PromptTemplate, template="{question}"), retriever=dict(type=ZeroRetriever), inferencer=dict(type=GenInferencer))
eval_cfg = dict(evaluator=None, pred_role="BOT")
pause_probe_datasets = [dict(abbr="pause-probe", type=CustomDataset, path="$case_dir/dataset_v2.jsonl", reader_cfg=reader_cfg, infer_cfg=infer_cfg, eval_cfg=eval_cfg)]
EOF
  cat > "$host_case/configs/models/vllm_api/pause_probe_on.py" <<'EOF'
from ais_bench.benchmark.models import VLLMCustomAPI
models = [dict(attr="service", type=VLLMCustomAPI, abbr="pause-probe-on", path="/home/RAG_data/models/Qwen3-8B", model="kvh", stream=True, use_chat_api=True, session_aware=True, session_concurrency=1, agent_hint_transport="header", pass_agent_hint=True, agent_status_report=True, extra_headers={}, json_extra_fields=["messages", "tools"], use_timestamp=False, batch_size=1, request_rate=0, retry=2, api_key="", host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1"), host_port=int(__import__("os").environ.get("AISBENCH_HOST_PORT", "18080")), url="", max_out_len=1, generation_kwargs=dict(temperature=0, ignore_eos=True))]
EOF
  start_mock "$case_dir/received.jsonl"
  run_aisbench "$case_dir" "" \
    "ais_bench --config-dir ${case_dir}/configs --models pause_probe_on --datasets pause_probe --mode perf --num-warmups 0 --work-dir ${case_dir}/run >/tmp/pause-run.log 2>&1; rc=\$?; echo AISBench exit=\$rc; tail -2 /tmp/pause-run.log; exit \$rc"
  local host_out
  host_out=$(find "$host_case/run" -path '*/logs/infer/*/*.out' | head -1 || true)
  local log_arg=""
  if [ -n "$host_out" ]; then
    log_arg="${host_out/"$ROOT"/\/workspace}"
  fi
  run_check "$case_dir" "$case_dir/dataset_v2.jsonl" "on" "$log_arg" "$case_dir/received.jsonl"
}

run_case on-example \
  vllm_api_general_stream_session_hint_v2_mock_on \
  custom_agent_session_hint_v2 \
  /workspace/ais_bench/datasets/agent/example_v2.jsonl \
  on
run_case off-example \
  vllm_api_general_stream_session_hint_v2_mock_off \
  custom_agent_session_hint_v2 \
  /workspace/ais_bench/datasets/agent/example_v2.jsonl \
  off
run_case on-smoke-finalize \
  vllm_api_general_stream_session_hint_v2_mock_on \
  swebench_trace_v2_smoke_finalize \
  "$DATA_DIR/swebench_smoke_v2_finalize.jsonl" \
  on
if [ "$QUICK" -ne 1 ]; then
  run_case on-full-running \
    vllm_api_general_stream_session_hint_v2_mock_on \
    swebench_trace_v2 \
    "$DATA_DIR/swebench_full50_v2_running.jsonl" \
    on
  run_case on-full-finalize \
    vllm_api_general_stream_session_hint_v2_mock_on \
    swebench_trace_v2_finalize \
    "$DATA_DIR/swebench_full50_v2_finalize.jsonl" \
    on
fi
run_pause_event_case
run_timestamp_case

docker rm -f agent-hint-mock >/dev/null 2>&1 || true
echo
echo "conformance results: $BASE"
