# Harness Capture (real tasks → durable JSON dataset)

This package adds the missing upstream half of Agent Hint v2: running a **real
harness** on real tasks, capturing its model requests (including the real
`X-Agent-Hint` header), and persisting them as a durable JSON dataset that the
existing v2 builder / replay path consumes.

Chinese usage guide:
`docs/source_zh_cn/advanced_tutorials/session_aware_agent_hint_v2_harness_capture.md`

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
Use `--forward-agent-status` to forward `/agent-status` to the upstream instead
of only recording it.

For task lists, prefer `batch` (see below).

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

- `build --status-events status.jsonl` uses real `turn/end(completed)` events
  to set `finalize`; without it, `--state-mode completed-finalize` only derives
  finalize from the last record. Incomplete sessions are reported separately.
- `build --tokenizer <path> --max-context <n>` validates
  `prompt(messages+tools+chat template) + max_out_len` and trims/drops
  overflow. Use a Python environment with `transformers` installed, or pass
  `--skip-tokenizer-check` to skip validation.
- The recorder parses streamed `usage` across chunk boundaries; `max_out_len`
  is the captured completion token count (capped by `--completion-token-cap`).
- The builder fills every optional column on every row (null when absent) so
  old `datasets.Dataset.from_list` versions do not drop later-only columns.
- DSH emits `pause`/`running` out-of-band on `status.jsonl`; the current V2
  replay consumes request-header `running`/`resume` plus merged `finalize`, not
  timed `pause` events. Replaying tool-wait pause/offload needs a timed status
  event channel (or `status_before/status_after`).
- Motor's current `cache_control` parser expects a dict, while the v2 header
  carries the spec's list form; the list is currently ignored (WARNING). The
  dataset is faithful; the consumer needs a small fix.
- Auxiliary calls (session-title, compaction) carry no hint; with `--hints on`
  they are dropped from the dataset so the replay never fabricates a hint the
  harness did not send.
- `off` capture uses the harness session header (`x-deepseek-harness-session-id`)
  so the dataset still groups sessions even without hints.

## DSH runtime setup (verified)

- Node >= 22 (24.22.0 used); @deepseek-ai/dsh@0.1.6-alpha.2.
- 
pm install @deepseek-ai/dsh@0.1.6-alpha.2 --ignore-scripts works for headless
  runs when the native koffi build is not needed.
- dsh plugin add needs a **Linux** pnpm on PATH (9.15.0 used) and
  ignore-workspace-root-check=true (profile .npmrc).
- Install the hint plugin once:
  dsh plugin --profile capture add /path/to/dsh-agent-hints.

## Dify one-time setup

Dify stores model endpoints in its own DB. Point the app's agent-hints provider
at the recorder (http://<host>:<port>/v1), then run
examples/dify_task.py through capture run --listen-port <port>. Switching
between hints on/off is a provider switch (agent-hints vs plain OpenAI-compatible).

## Batch production

`tasks.jsonl` (one JSON object per line):

```json
{"task_id":"t001","prompt":"...","turns":2}
```

```bash
python -m ais_bench.capture batch \
  --tasks tasks.jsonl \
  --harness-cmd "bash examples/dsh_task.sh" \
  --hints on --out captures/dsh_on_batch \
  --upstream-host <model-host> --upstream-port <model-port> \
  --upstream-model <served-model> \
  --env DSH_BIN=... --env DSH_HOME=... --env NODE_BIN=...
```

Output:

```
captures/dsh_on_batch/
  runs/<task_id>/{requests.jsonl,model_io/}
  model_io/                 # merged per-request model_io
  dataset_v2.jsonl          # Agent Hint v2 dataset
  dataset_v2.jsonl.report.json
  dataset.manifest.json     # harness/cmd/tasks/sessions/requests/coverage/sha256
```

Validate:

```bash
python -m ais_bench.capture validate --dataset captures/dsh_on_batch/dataset_v2.jsonl --hints on
```

Notes:
- hints on keeps only requests that actually carried a hint (auxiliary
  session-title/compaction calls are dropped so no hint is fabricated).
- hints off keeps all inference calls and groups them by the harness session
  header; the dataset has no priority/cache/prediction.
- Dify's provider endpoint is configured out of band: use a fixed
  `--listen-port` for the batch and point the provider at it once.
- The DSH example uses the hint provider for both modes; `--hints off` is the
  serving-side dataset without hints. A true harness-without-plugin run can use
  the `llm-pi-ai` route with `reasoningEfforts: false`.
- Unit tests: `pytest ais_bench/capture/tests`.

## Dify with agent-hints 0.1.7 + hint service

- The plugin (0.1.7) emits `session` / `priority` / `cache_control` /
  `agent_status` natively. Older deployments (0.1.4) do **not** emit
  `agent_status`; upgrade the Dify plugin to 0.1.7.
- `agent_type` / `prediction` / `call_index` come from the hint service:
  - compute each LLM node's prefix (sha256 of the first static system message,
    first 16 chars; `integrations/dify/dsl.py`),
  - `POST {hint_service}/v1/hints` with static entries per prefix,
  - run the workflow with the companion driver, which binds the run id and
    lifecycle state (`POST /v1/bindings`).
- Example: `examples/dify_hint_service.py` (env-driven).
- The app's agent-hints provider endpoint must point at the recorder
  (`hint-mock-model` -> `http://hint-mock:9999/v1`).
