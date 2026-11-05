# Harness Capture (real tasks → durable JSON dataset)

This package adds the missing upstream half of Agent Hint v2: running a **real
harness** on real tasks, capturing its model requests (including the real
`X-Agent-Hint` header), and persisting them as a durable JSON dataset that the
existing v2 builder / replay path consumes.

```
real harness (DSH / Dify / ...)
  │  hints on:  agent-hints provider emits X-Agent-Hint
  │  hints off: no hint, session id from the harness header
  ▼
ais_bench/capture/recorder.py        # recording proxy (headers/body/usage/timing)
  ▼
ais_bench.capture to-model-io        # capture.jsonl -> model_io/*.json
  ▼
ais_bench.capture build              # model_io -> Agent Hint v2 JSONL
  ▼
existing v2 replay configs (ON / OFF)
```

## Commands

### Run a harness and capture

```bash
PYTHONPATH=/path/to/benchmark \
python -m ais_bench.capture run \
  --harness-cmd "bash examples/dsh_two_turn.sh" \
  --hints on \
  --out captures/dsh_on \
  --upstream-host <motor-host> --upstream-port <motor-port> \
  --upstream-model <served-model> --run-id dsh-on-1
```

`run` starts the recorder on a free port, exports `CAPTURE_ENDPOINT` and
`CAPTURE_LOG` to the harness command, then writes `model_io/` automatically.

### Build the v2 dataset

```bash
python -m ais_bench.capture build \
  --input-dir captures/dsh_on/model_io \
  --output datasets/dsh_on_v2.jsonl \
  --builder capture --state-mode running
```

`--builder capture` preserves the full captured hint (session/agent_type/
call_index/priority/cache_control/prediction/agent_status). `--builder jiuwen`
uses the legacy Jiuwen model_io converter.

### Replay

Use the v2 model configs from the parent branch:

- ON:  `vllm_api_general_stream_session_hint_v2_trace`
- OFF: `vllm_api_general_stream_session_hint_v2_trace_off`

`use_timestamp=True` reproduces the captured arrival pacing; for capture
datasets built here it can compute a very long first delay, so controlled A/B
runs should set `use_timestamp=False`.

## Hints ON / OFF

| Mode | Harness | Dataset | Replay |
|---|---|---|---|
| ON | agent-hints provider (DSH `deepseek-hint`, Dify `agent-hints`) | full hint preserved | header transport + `pass_agent_hint=True` |
| OFF | same harness command | `to-model-io --hints off` (hint ignored) | `pass_agent_hint=False` |

The same captured requests can be replayed ON and OFF (only the model config
differs), which isolates hint delivery from workload differences. A true
harness-without-plugin run is possible with the `llm-pi-ai` provider; see the
DSH example notes.

## Notes / known gaps

- Motor's current `cache_control` parser expects a dict, while the v2 header
  carries the spec's list form; the list is currently ignored (WARNING). The
  dataset is faithful; the consumer needs a small fix.
- Auxiliary calls (session-title, compaction) carry no hint; with `--hints on`
  they are dropped from the dataset so the replay never fabricates a hint the
  harness did not send.
- `off` capture uses the harness session header (`x-deepseek-harness-session-id`)
  so the dataset still groups sessions even without hints.
