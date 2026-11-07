# Agent Hint V2 全流程测试 Playbook

本文是本仓库 Agent Hint V2 测试的统一参考手册，覆盖：

- 真实任务轨迹采集
- 数据集构造
- 数据集 hint 逻辑校验
- 轨迹回放
- 功能测试
- 性能测试
- 发送侧一致性校验
- KVC pause/offload/prefetch 测试
- 常见问题与排障

后续做数据构造、校验、功能或性能实验时，优先按本文的流程和命令执行。

> 本文针对 `feature/agent-hint-v2` 分支和 `X-Agent-Hint` Header 协议。
> v2 不是精度评测基准，数据集默认 `evaluator=None`。

---

## 1. 测试分层

```text
真实 harness
  │  X-Agent-Hint / /agent-status
  ▼
ais_bench.capture recorder
  │  requests.jsonl + status_events.jsonl
  ▼
model_io / v2 数据集
  │  静态校验 + tokenizer 上下文校验 + 发送侧 conformance
  ▼
AISBench 回放
  │  session-aware / ON-OFF / delay_after_ms / timestamp
  ▼
Motor + vLLM
  │  亲和、生命周期、finalize evict、pause offload/prefetch
  ▼
功能检查 + 性能报告
```

| 阶段 | 主要工具 | 是否需要 GPU |
|---|---|---|
| 采集 | `ais_bench/capture/` | 否（需要上游 Motor/vLLM） |
| 构造 | `build_agent_dataset_v2_from_model_io.py`、`build_agent_dataset_v2_pause.py`、`ais_bench.capture build` | 否（tokenizer 校验可选） |
| 校验 | `validate_agent_hint_dataset.py`、`ais_bench.capture validate`、`check_send_side.py` | 否 |
| 回放 | AISBench `--mode perf` | 是（真实引擎时） |
| 功能 | AISBench + Motor + vLLM | 是 |
| 性能 | AISBench + Motor + vLLM | 是 |
| KVC 功能 | `tests/manual/kvc_management/` | 是 |

---

## 2. 环境与目录约定

### 2.1 镜像与模型

常用本地资产：

```text
Engine 镜像:  agenthint-vllm:0.1.0-p3memcache
Motor 镜像:   mindie-motor-vllm:e2e3b
AISBench 镜像: ais-runner:ais-1
模型:         /home/RAG_data/models/Qwen3-8B
切片数据:     /home/t00887700/benchmark-data/agent_replay_v2
DSH 抓包:     /home/t00887700/benchmark-data/agent_capture_dsh/current
本地 sat:     /home/t00887700/kv-hint/.build/aisbench/data
```

### 2.2 环境变量

配置文件和工具统一支持以下环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_HINT_DATA_DIR` | `/home/t00887700/benchmark-data/agent_replay_v2` | v2 回放数据集 |
| `AGENT_HINT_CAPTURE_DIR` | `/home/t00887700/benchmark-data/agent_capture_dsh/current` | 真实 harness 抓包 |
| `AGENT_HINT_LOCAL_DATA_DIR` | `/home/t00887700/kv-hint/.build/aisbench/data` | sat/quick 本地数据 |
| `AGENT_HINT_EXAMPLE_V2` | `ais_bench/datasets/agent/example_v2.jsonl` | 合成生命周期样例 |
| `AISBENCH_HOST_IP` | 各配置默认 | Motor/引擎地址 |
| `AISBENCH_HOST_PORT` | 各配置默认 | Motor/引擎端口 |
| `AISBENCH_AGENT_HINT_AUDIT_PATH` | 未设置 | 开启发送侧审计 JSONL |
| `AGENT_HINT_MOCK_PORT` | `18080` | mock conformance 服务端口 |
| `AIS_IMAGE` | `ais-runner:ais-1` | 无 GPU CI 使用的镜像 |

> AISBench 的 mmengine config 使用 lazy-import，配置文件中不能直接
> `import os` 后调用 `os.environ.get`；本仓库统一使用
> `__import__("os").environ.get(...)` 读取环境变量。

### 2.3 端口与卡隔离

做实验前先检查端口和卡占用：

```shell
npu-smi info
ss -ltnp | grep -E ':(8100|19025|19125|19225|19325|1025|1026|1027)$'
docker ps --format '{{.Names}}\t{{.Status}}'
```

推荐约定：

| 用途 | 端口 |
|---|---|
| 基础 AISBench 快速配置 | Motor `19025/19026/19027` |
| 隔离 KVC/功能栈 | engine `18000`，Motor `19125/19126/19127` |
| 另一套隔离栈 | engine `8100`，Motor `19225/19226/19227` 或 `19325/19326/19327` |

不要复用其他实验的端口；`AISBENCH_HOST_PORT` 可以在不修改配置文件的情况下切换 Motor 端口。

---

## 3. 快速开始

### 3.1 无 GPU 全链路 CI（推荐先跑）

```shell
cd /home/t00887700/benchmark
bash tests/agent_hint/run_agent_hint_ci.sh
```

快速模式（跳过 full50）：

```shell
QUICK=1 bash tests/agent_hint/run_agent_hint_ci.sh
```

它会依次执行：

1. 数据 manifest
2. 静态数据集校验
3. 57 个单测
4. mock 发送侧一致性
5. pause 事件回放
6. session-aware timestamp pacing
7. mock ON/OFF 报告

通过时最后输出：

```text
Agent Hint CI PASS
```

### 3.2 真实 Motor + vLLM 功能链路

真实栈需要自行启动 Motor 和 engine。基础启动可参考
`session_aware_agent_hint_v2.md` 第 5 节；KVC 隔离栈可使用
`tests/manual/kvc_management/scripts/start_stack.sh`。最小检查顺序：

```shell
# 1. 启动 engine（示例：卡 0/1，TP2，本地 HBM 模式）
# 2. 启动 Motor，确认 19225/19226/19227 或 19325/19326/19327
# 3. 注册 engine
curl -X POST http://127.0.0.1:19326/instances/refresh \
  -H 'Content-Type: application/json' \
  -d '{
    "event": "set",
    "model_name": "kvh",
    "dispatch_capabilities": "concurrent_engine_sync",
    "instances": [
      {"id": 1, "role": "prefill", "endpoints": [{"id": 0, "address": "127.0.0.1:8100"}]},
      {"id": 2, "role": "decode",  "endpoints": [{"id": 0, "address": "127.0.0.1:8100"}]}
    ]
  }'

curl http://127.0.0.1:19326/readiness
```

> 启动 Motor 前务必确认端口未被占用。端口冲突时 Motor 会直接退出，AISBench 可能误连到旧 Motor。

---

## 4. 数据构造

### 4.1 真实 harness 抓包

统一入口：

```shell
python -m ais_bench.capture <run|record|to-model-io|build|batch|validate>
```

单次 DSH 抓包：

```shell
PYTHONPATH=/home/t00887700/benchmark \
python -m ais_bench.capture run \
  --harness-cmd "bash ais_bench/capture/examples/dsh_two_turn.sh" \
  --hints on \
  --out captures/dsh_on \
  --upstream-host 127.0.0.1 --upstream-port 19325 \
  --upstream-model kvh \
  --run-id dsh-on-1
```

批量真实任务：

```shell
python -m ais_bench.capture batch \
  --tasks tasks.jsonl \
  --harness-cmd "bash ais_bench/capture/examples/dsh_task.sh" \
  --hints on \
  --out captures/dsh_batch \
  --upstream-host 127.0.0.1 --upstream-port 19325 \
  --upstream-model kvh \
  --env DSH_BIN=... --env DSH_HOME=...
```

产物：

```text
captures/<run>/
├── requests.jsonl
├── status_events.jsonl
├── model_io/
├── dataset_v2.jsonl
├── dataset_v2.jsonl.report.json
└── dataset.manifest.json
```

要点：

- `--hints on` 只保留真实带 `X-Agent-Hint` 的请求，辅助调用会被丢弃。
- `--hints off` 按 `x-deepseek-harness-session-id` 分组。
- 真实抓包可能包含业务数据和工具输出，注意目录权限和脱敏。

### 4.2 从 model_io 构建 v2 数据集

Jiuwen legacy trace：

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py \
  --input-dir /path/to/model_io \
  --output /path/to/dataset_v2.jsonl \
  --state-mode running \
  --max-context 32768
```

`--state-mode`：

- `running`：保持 running。
- `completed-finalize`：按最后一条 completed inference 派生 finalize。

### 4.3 从 finalize 数据集构造 pause/offload 数据集

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_pause.py \
  --input /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl \
  --pause-every 3 \
  --min-session-requests 3 \
  --idle-offload-s 30 \
  --delay-ms 40000 \
  --tokenizer /home/RAG_data/models/Qwen3-8B \
  --max-context 32768
```

当前基线：

| 项 | 值 |
|---|---|
| 输入 / 输出 rows | 331 / 324 |
| 上下文丢弃 | 7 |
| sessions | 58 |
| pause / resume | 65 / 65 |
| 有 pause 的 session | 20 |
| 最大 header 长度 | 308 |
| `checks.all_passed` | true |

> pause 必须放在 session 第 N 个请求（1-based），下一请求改为 resume；
> 最后一行不会被改成 resume；不伪造 finalize。

### 4.4 真实抓包 pause 事件回放

如果已有真实 `status_events.jsonl`，可以直接生成 pause/resume 行：

```shell
python -m ais_bench.capture build \
  --input-dir captures/my_run/model_io \
  --output captures/my_run/dataset_pause.jsonl \
  --builder capture \
  --state-mode completed-finalize \
  --status-events captures/my_run/status_events.jsonl \
  --replay-pause-events
```

行为：

- pause/tool 事件映射到前一个请求：该行 `agent_status.state=pause`，写入 `delay_after_ms`。
- 下一请求写入 `resume_before=True`。
- AISBench 在 pause 行请求完成后上报 pause，等待 `delay_after_ms`，再在下一请求前上报 running。
- 连续 pause 事件也支持：同一行可以既是上一段 pause 的 resume，也是下一段 pause 的开始。

### 4.5 合成生命周期数据

`ais_bench/datasets/agent/example_v2.jsonl` 覆盖：

```text
running -> pause -> resume -> finalize
```

适合验证 Header 字段、`/agent-status`、`cache_control`、priority 和 KVC finalize。

---

## 5. 数据校验

### 5.1 数据 manifest

```shell
python3 tests/agent_hint/data_manifest.py --required core
```

输出每个数据集的路径、是否存在、大小，并给出 `ok/missing_required`。

### 5.2 静态 hint 逻辑校验

```shell
python3 ais_bench/datasets/agent/validate_agent_hint_dataset.py \
  ais_bench/datasets/agent/example_v2.jsonl \
  /home/t00887700/benchmark-data/agent_replay_v2/swebench_smoke_v2_running.jsonl \
  /home/t00887700/benchmark-data/agent_replay_v2/swebench_smoke_v2_finalize.jsonl \
  /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_running.jsonl \
  /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl
```

覆盖：

- 字段白名单、类型、取值域
- `session` / `agent_status` / `priority` / `strict_priority` / `cache_control` / `prediction`
- session 状态机、finalize 终态、epoch/call_index/timestamp 单调
- Header 长度 16384
- `cache_control` 只允许单个 dict，list 会失败
- `messages` / `tools` JSON 合法性
- `delay_after_ms` / `resume_before` 控制字段

当前结果：5 份数据集全部 PASS；`example_v2` 有 3 个预期 warning。

### 5.3 Capture 数据集校验

```shell
python -m ais_bench.capture validate \
  --dataset captures/my_run/dataset_v2.jsonl \
  --hints on \
  --require-finalize
```

### 5.4 发送侧一致性校验

AISBench 支持可选审计：

```shell
export AISBENCH_AGENT_HINT_AUDIT_PATH=/path/to/audit.jsonl
```

跑完后：

```shell
python3 ais_bench/datasets/agent/check_send_side.py \
  --dataset /path/to/dataset.jsonl \
  --audit /path/to/audit.jsonl \
  --expect on
```

校验点：

- 实际发送的 Header 与数据集逐字段一致
- base64url 完全一致
- body 不含 `agent_hint`
- `/agent-status` 数量、顺序、`applied`
- resume/running 在请求前，pause/finalize 在请求后

无 GPU 的完整发送侧 conformance：

```shell
bash tests/agent_hint/run_send_side_conformance.sh
```

覆盖：

| 用例 | 结果 |
|---|---|
| example ON | 6 headers / 4 status |
| example OFF | 0 headers / 0 status |
| smoke finalize | 13 headers / 5 status |
| full running | 331 headers / 0 status |
| full finalize | 331 headers / 51 status |
| pause-event replay | 4 headers / 5 status |
| timestamp pacing | 6s 时间戳，实测约 5.99s |

### 5.5 上下文校验

构造阶段必须校验：

```text
prompt_tokens(messages + tools + chat template) + max_out_len <= max_model_len
```

- `build_agent_dataset_v2_pause.py` 和 `ais_bench.capture build --tokenizer` 已内置。
- 不满足时下调 `max_out_len` 或丢弃，不能直接用抓包 `usage.input_tokens` 推算。

---

## 6. 功能测试

### 6.1 ON/OFF 配置

| 模式 | 模型配置 | `pass_agent_hint` | `agent_status_report` |
|---|---|---|---|
| ON | `vllm_api_general_stream_session_hint_v2_e2e_on` | True | True |
| OFF | `vllm_api_general_stream_session_hint_v2_e2e_off` | False | False |
| Trace ON | `vllm_api_general_stream_session_hint_v2_trace` | True | True |
| Trace OFF | `vllm_api_general_stream_session_hint_v2_trace_off` | False | False |
| DSH ON/OFF | `..._dsh_capture_on/off` | True/False | True/False |

### 6.2 推荐功能用例

```shell
# 1) 生命周期：running -> pause -> resume -> finalize
AISBENCH_HOST_PORT=19325 \
ais_bench --models vllm_api_general_stream_session_hint_v2_e2e_on \
          --datasets custom_agent_session_hint_v2 \
          --mode perf --num-warmups 0

# 2) OFF 对照
AISBENCH_HOST_PORT=19325 \
ais_bench --models vllm_api_general_stream_session_hint_v2_e2e_off \
          --datasets custom_agent_session_hint_v2 \
          --mode perf --num-warmups 0

# 3) 真实报文 + finalize
AISBENCH_HOST_PORT=19325 \
ais_bench --models vllm_api_general_stream_session_hint_v2_e2e_on \
          --datasets swebench_trace_v2_smoke_finalize \
          --mode perf --num-warmups 0
```

### 6.3 预期日志

AISBench：

```text
[agent_hint][send-header] ... state=running/pause/resume/finalize
[agent_hint][agent-status] ... state=pause/running/finalize
```

Motor：

```text
scheduled ... aff=fresh
scheduled ... aff=hit
agent_status event ... applied=True
kvc manage dispatch ... edit=evict
kvc evict done ... success=1
agent_status finalize released session pins ... dropped=1
```

Engine：

```text
Processing manage_request edits ...
Context management result ... type evict status True
```

### 6.4 KVC 手工功能套件

本地手工 KVC 套件：

```shell
cd /home/t00887700/benchmark

# 预检
bash tests/manual/kvc_management/qa_run.sh doctor

# 全部场景 fast（约 25-35 分钟）
bash tests/manual/kvc_management/qa_run.sh all --fast

# 单个场景
bash tests/manual/kvc_management/qa_run.sh case s3 --fast
```

场景：

| 场景 | 内容 |
|---|---|
| S1 | 压满基线（KVC off） |
| S2 | finalize -> evict |
| S3/S4 | pause -> offload -> resume -> prefetch |
| S5 | 压满触发（high_water=0.7） |
| S6 | KVC off 对照 |
| S7 | high-water gate |

详细步骤见 `tests/manual/kvc_management/README.md`。
已有执行报告见 `tests/manual/kvc_management/kvc_manual_execution_report_20261105.md`。

### 6.5 功能验收清单

- [ ] AISBench 出现 `[agent_hint][send-header]`
- [ ] pause/resume/finalize 的 `/agent-status` 返回 `applied=true`
- [ ] Motor 出现 `aff=fresh -> aff=hit`
- [ ] finalize 后 Motor 下发 evict，engine 返回 `status True`
- [ ] OFF 无 Header/status，Motor `prio=None aff=-`
- [ ] OFF 仍保持本地 session 串行
- [ ] 发送侧 checker PASS
- [ ] 性能报告包含 TTFT/E2EL/TPOT

---

## 7. 性能测试

### 7.1 基本原则

- 同一数据集、同一模型配置、同一 `session_concurrency`、同一 warmup。
- ON/OFF 只切换 `pass_agent_hint` / `agent_status_report`。
- 每臂之间重启 Motor；必要时重启 engine，避免 prefix cache 污染。
- 至少重复 3 次，关注 P90/P99。
- 真实 trace 受控 A/B 建议 `use_timestamp=False`，用 `session_concurrency` ramp。

### 7.2 常用配置

| 目的 | 模型配置 | 数据集配置 |
|---|---|---|
| 真实 trace ON/OFF | `..._trace` / `..._trace_off` | `swebench_trace_v2` |
| 快速 trace | `..._trace_quick` / `..._trace_quick_off` | `swebench_trace_v2_smoke_finalize_quick` |
| 并发 ramp | `..._trace_sat_on/off` | `swebench_trace_v2_sat_ramp` |
| KVC pause/offload | `..._pause_offload_c8/12/16/24/32` | `swebench_trace_v2_pause_offload` |
| DSH 真实抓包 | `..._dsh_capture_on/off` | `dsh_capture_v2` |

`SAT_C` 可以控制 `trace_sat_on/off` 的并发：

```shell
SAT_C=16 AISBENCH_HOST_PORT=19025 \
ais_bench --models vllm_api_general_stream_session_hint_v2_trace_sat_on \
          --datasets swebench_trace_v2_sat_ramp --mode perf
```

### 7.3 KVC pause/offload 压测

Motor KVC-on：

```text
agent_status_config.enabled=true
agent_status_config.central_lifecycle=true
kvc_lifecycle_config.enabled=true
kvc_lifecycle_config.idle_offload_s=30
kvc_lifecycle_config.idle_evict_s=0
kvc_lifecycle_config.offload_kv_usage_high_water=0.7
kvc_lifecycle_config.prefetch_enabled=true
```

KVC-off 对照：

```text
agent_status_config.enabled=true
kvc_lifecycle_config.enabled=false
```

运行：

```shell
AISBENCH_HOST_PORT=19325 \
ais_bench --models vllm_api_general_stream_session_hint_v2_pause_offload_c16 \
          --datasets swebench_trace_v2_pause_offload \
          --mode perf
```

关注：

- `motor:kvc_offload_total`
- `motor:kvc_prefetch_total`
- `motor:kvc_evict_total`
- `motor:kvc_edit_success_total`
- `motor:kvc_edit_fail_total`
- `motor:kvc_gate_skip_total`
- engine `kv_cache_usage_perc`、Waiting、preemptions

### 7.4 报告工具

单次 ON/OFF 报告：

```shell
python3 tests/agent_hint/report_perf.py \
  --on-run outputs/agent-hint/on \
  --off-run outputs/agent-hint/off \
  --motor-before on/motor-before.prom \
  --motor-after on/motor-after.prom \
  --out outputs/agent-hint/report
```

或对已运行 Motor 自动跑 ON/OFF：

```shell
AISBENCH_HOST_PORT=19325 \
MOTOR_METRICS_URL=http://127.0.0.1:19327/metrics \
AGENT_HINT_DATA_DIR=/home/t00887700/benchmark-data/agent_replay_v2 \
bash tests/agent_hint/run_perf_ab.sh
```

产物：

```text
<out>/report.md
<out>/report.json
```

### 7.5 性能验收

- [ ] 数据集、并发、warmup、引擎配置完全一致
- [ ] ON/OFF 各至少 1 次，推荐 3 次交替
- [ ] 报告包含 TTFT/E2EL/TPOT/吞吐/并发
- [ ] Motor KVC 计数符合预期
- [ ] `edit_fail=0`、`send_fail=0`
- [ ] 如果无收益，明确写“该负载下无收益”，不要强行归因

---

## 8. 本轮全流程测试记录（2026-11-07）

### 8.1 无 GPU CI

命令：

```shell
bash tests/agent_hint/run_agent_hint_ci.sh
```

结果：`Agent Hint CI PASS`

- 单测：57 passed
- 静态校验：5 份数据集 PASS
- mock conformance：
  - example ON：6 headers / 4 status
  - example OFF：0 headers / 0 status
  - smoke finalize：13 headers / 5 status
  - full running：331 headers / 0 status
  - full finalize：331 headers / 51 status
  - pause-event replay：4 headers / 5 status
  - timestamp pacing：6s 时间戳，实测 delta 5.989s
- mock ON/OFF 报告已生成

结果目录：

```text
outputs/agent-hint-ci/conformance/
```

### 8.2 真实 Motor + vLLM

拓扑：

```text
卡 0/1，单 TP2 engine，本地 HBM 模式
engine: 127.0.0.1:8100
Motor:  127.0.0.1:19325/19326/19327
```

结果：

| 用例 | 请求 | 成功/失败 | 发送侧 checker | 关键观测 |
|---|---|---|---|---|
| ON example_v2 | 6 | 6/0 | PASS 6 headers / 4 status | TTFT avg 170.1ms，aff=fresh→hit，finalize evict success |
| OFF example_v2 | 6 | 6/0 | PASS 0 headers / 0 status | TTFT avg 114.0ms，prio=None aff=- |
| ON smoke finalize | 13 | 12/1 | PASS 13 headers / 5 status | 1 条 context 400；5 个 finalize evict 成功 |

结果目录：

```text
outputs/agent-hint-e2e/real-on-example-20261107_143849
outputs/agent-hint-e2e/real-off-example-20261107_144056
outputs/agent-hint-e2e/real-on-smoke-20261107_144305
```

说明：

- smoke finalize 的 1 个失败是输入 31745 tokens + 输出 1024 tokens 超过 32768，
  属于上下文预算问题，不是 Agent Hint 发送或调度问题。
- 本轮 ON/OFF 仅用于功能验证，样本很小，不作为最终性能结论。
- 本轮曾遇到 `19225` 被其他实验 Motor 占用，Motor 启动失败但 AISBench 误连旧
  Motor，最终改用 `19325` 解决。做实验前务必检查端口。

### 8.3 KVC 功能

已有 KVC 手工套件报告（2026-11-05 / 2026-11-06）：

- fast 模式 6/6 PASS
- S2 finalize → evict：2 finalize → 2 evict / 333 blocks
- S3 pause → offload → prefetch：3 → 3 → 3，3 evict / 1519 blocks
- S6 KVC off：生命周期正常，KVC/engine edit 全 0
- S7 high-water gate：gate_skip=2，offload=0

参考：

```text
tests/manual/kvc_management/kvc_manual_execution_report_20261105.md
tests/manual/kvc_management/results/QA_REPORT_latest.md
```

---

## 9. 常见问题与排障

### Q1. Motor 启动后立刻退出，AISBench 返回 Service Unavailable

先检查端口：

```shell
ss -ltnp | grep -E ':(19225|19325|8100)$'
docker logs ah-motor | tail -40
```

如果端口被其他实验占用，改用：

```shell
AISBENCH_HOST_PORT=19325
```

并确保 Motor 自己监听的是 19325。

### Q2. pause/resume 返回 `applied=false reason=terminal`

`finalize` 是终态。每轮 KVC-on 实验前重启 Motor，或使用新的 `session_id` /
更大的 `epoch`。

### Q3. AISBench 在 ais-runner 中段错误

部分环境 `av` + `torch` 会段错误。CI 和手工脚本已统一执行：

```shell
pip uninstall -y av
```

### Q4. 请求返回 400 / maximum context length

用 tokenizer 校验：

```text
prompt_tokens(messages + tools + chat template) + max_out_len <= max_model_len
```

下调 `max_out_len` 或提高引擎 `max-model-len`；不要用抓包 `usage.input_tokens` 直接推算。

### Q5. 修改配置后 mmengine 报 invalid syntax / RuntimeError

配置使用 lazy-import，不能直接 `import os` 后调用 `os.environ.get`。
使用：

```python
host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1")
```

### Q6. cache_control 是否支持 list

当前只支持单个 dict。不要为 Motor 回放生成 list；静态校验器会报错。
Capture 层可以保留原始抓包，但用于回放的 v2 数据集必须是单 dict。

### Q7. KVC 没有 offload / prefetch

检查：

- Motor `kvc_lifecycle_config.enabled=true`
- `agent_status_config.enabled=true`
- `MOTOR_SESSION_AFFINITY=1`（KVC 需要 location pin）
- pause 后等待时间是否大于 `idle_offload_s`
- `offload_kv_usage_high_water` 是否 gate 住
- Motor 日志 `kvc offload/prefetch done`
- engine 日志 `Context management result`

### Q8. 真实抓包的 pause 事件没有回放

使用：

```shell
python -m ais_bench.capture build ... \
  --status-events <status_events.jsonl> \
  --replay-pause-events
```

并确认生成的 pause 行有 `delay_after_ms`，下一行有 `resume_before=True`。

### Q9. 能否只看发送侧，不依赖真实 GPU

可以：

```shell
bash tests/agent_hint/run_send_side_conformance.sh
```

它使用 mock OpenAI 服务，验证 Header/status/body/timestamp/pause 回放，不需要 NPU。

---

## 10. 文件索引

| 文件 | 作用 |
|---|---|
| `ais_bench/capture/` | 真实 harness 抓包、model_io、v2 构建、batch |
| `ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py` | Jiuwen trace 转 v2 |
| `ais_bench/datasets/agent/build_agent_dataset_v2_pause.py` | finalize 数据集转 pause/offload |
| `ais_bench/datasets/agent/validate_agent_hint_dataset.py` | 静态 hint/schema/状态机校验 |
| `ais_bench/datasets/agent/check_send_side.py` | 发送侧 Header/status 校验 |
| `ais_bench/datasets/agent/mock_agent_hint_server.py` | 无 GPU mock 服务 |
| `ais_bench/benchmark/utils/agent_hint.py` | Header 编码、归一化、审计 |
| `ais_bench/benchmark/configs/models/vllm_api/*session_hint_v2*` | Agent Hint 模型配置 |
| `ais_bench/benchmark/configs/datasets/custom/*trace*` | Agent Hint 数据集配置 |
| `tests/agent_hint/run_agent_hint_ci.sh` | 无 GPU 全链路 CI |
| `tests/agent_hint/run_send_side_conformance.sh` | mock 发送侧 conformance |
| `tests/agent_hint/data_manifest.py` | 数据 manifest |
| `tests/agent_hint/report_perf.py` | ON/OFF 性能报告 |
| `tests/agent_hint/run_perf_ab.sh` | 自动 ON/OFF 性能运行 |
| `tests/manual/kvc_management/` | KVC 手工功能套件 |
| `tests/manual/ttft_issue52113/` | TTFT 问题手工实验套件 |

---

## 11. 新实验记录模板

复制以下模板记录每次实验：

```markdown
## 实验记录 YYYY-MM-DD

- 分支/commit:
- 镜像: engine=..., Motor=...
- 模型:
- 拓扑: 单/多引擎，TP，PD 模式，卡号
- Motor KVC: on/off，idle_offload_s，high_water，prefetch
- 数据集: 名称、rows、sessions、pause/resume/finalize 数
- 数据集校验: manifest / static / context / send-side
- 回放配置: ON/OFF，session_concurrency，use_timestamp，warmup
- 功能结果: AISBench 发送日志、Motor applied/aff、engine edit、checker
- 性能结果: TTFT P50/P90/P99、E2EL、TPOT、吞吐、KV usage、Waiting
- 异常与处理:
- 结论:
- 证据目录:
```
