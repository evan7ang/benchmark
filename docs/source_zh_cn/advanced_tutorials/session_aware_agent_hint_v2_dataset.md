# Agent Hint V2 真实回放数据集使用说明

本文说明如何生成、选择和使用由 JiuwenSwarm SWE-bench 抓包转换得到的 Agent Hint V2 数据集。

## 1. 数据集位置

数据文件位于：

```text
/home/t00887700/benchmark-data/agent_replay_v2/
```

| 文件 | 大小 | 记录数 | session 数 | 说明 |
|---|---:|---:|---:|---|
| `swebench_full50_v2_running.jsonl` | 34 MB | 331 | 58 | 只回放抓包可证明的 `running` |
| `swebench_full50_v2_finalize.jsonl` | 34 MB | 331 | 58 | 280 `running` + 51 派生 `finalize` |
| `swebench_smoke_v2_running.jsonl` | 1.4 MB | 13 | 5 | 冒烟子集 |
| `swebench_smoke_v2_finalize.jsonl` | 1.4 MB | 13 | 5 | 冒烟子集 + 5 个派生 `finalize` |

每个 JSONL 旁边有对应的 `*.report.json`，记录转换统计和跳过原因。

## 2. 数据来源

源数据：

```text
/home/t00887700/benchmark-output/full50/on/model_io/*.json
```

- 567 条记录
- 332 条 inference
- 235 条 management
- 59 个 provider session

这是 JiuwenSwarm 在 SWE-bench 场景下实际发给 Motor 的请求抓包。它使用的是 **legacy body `agent_hint`**，不是新版 `X-Agent-Hint` Header 抓包。

转换脚本：

```text
ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py
```

生成命令：

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py \
  --input-dir /home/t00887700/benchmark-output/full50/on/model_io \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_running.jsonl
```

## 3. hint 字段口径

| 字段 | 来源 | 是否合成 |
|---|---|---|
| `session.session_id` | `request.agent_hint.session_id` | 否 |
| `session.parent_session_id` | `request.agent_hint.parent_session_id` | 否 |
| `agent_status.state` | `running`；finalize 变体按最后一条 completed inference 派生 | 部分派生，见下文 |
| `priority` / `strict_priority` | 抓包中不存在 | 不合成 |
| `cache_control` | 抓包中不存在 | 不合成 |
| `epoch` / `call_index` / `derivation` | 抓包中不存在 | 不合成 |
| `prediction` | 抓包中不存在 | 不合成 |
| `messages` / `tools` | 原始请求体，JSON 字符串保存 | 否 |
| `timestamp` | `timing.source_utc.start_time` | 否 |
| `max_out_len` | `usage.completion_tokens`，按上下文和 cap 截断 | 派生，用于回放形状 |

实际 `agent_hint` 结构只有：

```json
{
  "session": {
    "session_id": "...",
    "parent_session_id": "..."
  },
  "agent_status": {
    "state": "running|finalize"
  }
}
```

准确性说明：

- `session_id` / `parent_session_id` 全部来自抓包，0 处不一致。
- 202 条记录的 `parent_session_id == session_id`；129 条为子会话或 compressor 子会话；所有非自身 parent 都能在数据集中找到对应 session。
- running 版本全部是 `running`，没有派生 finalize。
- finalize 版本有 51 个 finalize，全部满足：
  - 是该 session 在数据集中的最后一行；
  - 是源数据中该 session 的最后一条 inference；
  - 源请求 `trace_status=completed`、`terminal_reason=completed`。
- 其余 7 个 session 保持 `running`，因为源数据最后一条 inference 分别为 failed（4）、in_flight（2）、cancelled（1）。
- 1 个只包含单条 compressor 请求的 session 因该请求超出 32k 上下文预算被整体排除，因此没有为它伪造 finalize。
- `turn_num` 在抓包中恒为 1，没有作为 `call_index` 使用。
- 235 条 management/evict 请求被丢弃；v2 Header 没有 `manage_request` 对应物。
- 105 条 compressor 子会话 inference 被保留。
- `messages` / `tools` 以 JSON 字符串保存，避免 Arrow struct 补 `None`；请求时由 `json_extra_fields` 解析回对象。
- Header 最大长度：running 307 字符，finalize 308 字符，远小于 Motor 的 16384 上限。

## 4. 数据集怎么选

| 目标 | 数据集 |
|---|---|
| 真实负载、忠实回放、不推断生命周期 | `swebench_full50_v2_running.jsonl` |
| 真实负载 + 测 finalize 释放 pin | `swebench_full50_v2_finalize.jsonl` |
| 快速验证链路 | `swebench_smoke_v2_running.jsonl` / `swebench_smoke_v2_finalize.jsonl` |
| 测 pause / resume / cache_control / priority | `ais_bench/datasets/agent/example_v2.jsonl` |

注意：真实抓包中没有 `pause` / `resume`。在 legacy 协议里 `pause -> offload`、`resume -> prefetch`，但这份 trace 的 management 命令全部是 `evict`，没有 `offload` / `prefetch`，也没有 `session_control`。因此真实数据集不包含 pause/resume；功能测试请使用 `example_v2.jsonl`。

## 5. 使用前准备

### 5.1 Motor 配置

需要会话亲和时：

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "127.0.0.1"
    },
    "agent_status_config": {
      "enabled": true,
      "session_ttl_s": 3600,
      "max_sessions": 65536
    }
  }
}
```

环境变量：

```shell
export MOTOR_SESSION_AFFINITY=1
export MOTOR_SESSION_AFFINITY_SHARED=1
export MOTOR_SESSION_AFFINITY_PLACEMENT=balanced
```

- 单引擎 PD 混部可以使用 `concurrent_engine_sync` 注册。
- 多引擎请按实际 PD 拓扑部署，并确保同 session 能命中 `aff=hit`。
- 引擎 `--max-model-len` 建议不小于 32768；如果显存不足，需要用转换脚本的 `--max-context` 与引擎保持一致。

### 5.2 修改模型配置

编辑：

```text
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_trace.py
```

至少修改：

```python
path="/path/to/tokenizer",
model="qwen3-8b",
host_ip="<motor-infer-ip>",
host_port=<motor-infer-port>,
```

OFF 对照使用：

```text
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_trace_off.py
```

两个配置的关键差异：

```python
# ON
pass_agent_hint=True
agent_status_report=True

# OFF
pass_agent_hint=False
agent_status_report=False
```

`json_extra_fields` 和 `drop_none_extra_fields` 是解析 `messages` / `tools` JSON 字符串所必需的，不要删除。

### 5.3 数据集路径

数据集配置：

```text
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_finalize.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_smoke.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_smoke_finalize.py
```

这些配置硬编码了 `/home/t00887700/benchmark-data/agent_replay_v2/...`。如果移动数据，请同步修改配置文件中的 `path`。

## 6. 运行方式

### 6.1 全量 ON

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2_trace \
          --datasets swebench_trace_v2 \
          --mode perf
```

### 6.2 全量 OFF

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2_trace_off \
          --datasets swebench_trace_v2 \
          --mode perf
```

### 6.3 冒烟

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2_trace \
          --datasets swebench_trace_v2_smoke_finalize \
          --mode perf
```

### 6.4 时间戳回放

模型配置默认 `use_timestamp=True`，按原始请求到达时间回放：

- 全量 running/finalize：约 4.5 小时
- smoke：约 20 分钟

如果只想快速做受控 ON/OFF 对比，复制一份模型配置并把 `use_timestamp` 改为 `False`，同时保持两组 `session_concurrency` 和 warmup 一致。

原始 full50 抓包的并发为 2，因此 trace 模型配置默认：

```python
session_concurrency=2
batch_size=2
```

## 7. 预期日志

AISBench：

```text
[agent_hint][send-header] data_id=... session_id=... state=running|finalize
[session-aware] session ... START/END request ...
[agent_hint][agent-status] ... state=finalize ...   # finalize 数据集
```

Motor：

```text
scheduled role=decode ... prio=None aff=fresh ...
scheduled role=decode ... prio=None aff=hit ...
agent_status event session_id=... state=finalize epoch=0 applied=True
```

注意：这份 trace 的 hint 没有 `priority`，因此 Motor 日志里的 `prio` 为空或 `None`，这是正常的；不代表 Header 没生效，关键看 `aff=fresh -> aff=hit`。

## 8. 结果解读

- 性能指标看 `performances/**/*.json` 的 TTFT / E2EL / TPOT。
- 逐请求明细看 `*_details.jsonl`。
- 由于真实 trace 没有 `call_index`，建议按 `session_id` + 请求顺序（或 `data_id`）分组看逐轮 TTFT。
- ON/OFF 对比必须使用同一数据集、同一 `session_concurrency`、同一 warmup、同一引擎配置，并建议重复至少 3 轮看 P90/P99。

## 9. 验证与审计结论

已完成的验证：

- 用源抓包逐字段审计：
  - 331 条数据全部匹配源 inference；
  - `session_id` / `parent_session_id` / `messages` / `tools` / 采样参数 / `max_out_len` / `timestamp` 0 处不一致；
  - finalize 放置 0 处问题；
  - 没有未预期的 hint 字段。
- 全量 running 数据集用 HF datasets 加载：331 行、58 session。
- smoke finalize 数据集跑 AISBench CLI + mock 服务：
  - 13 条数据集请求 + 1 条 warmup；
  - 0 失败；
  - 每条请求都有 `X-Agent-Hint`；
  - body 中没有 `agent_hint`；
  - `messages` / `tools` 正确解析；
  - 发送了 5 个 finalize `/agent-status` 事件；
  - Max Concurrency = 2。
- 单元测试 133 项通过。

未完成：

- 真实 trace 数据集尚未在 NPU + Motor 上跑全量；目前只做了数据审计和 mock 冒烟。
- 之前的 NPU + Motor 联调用的是合成 `example_v2.jsonl`。

## 10. 重新生成

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--state-mode` | `running` | `running` 或 `completed-finalize` |
| `--session-limit` | 无 | 只保留前 N 个 session |
| `--max-records` | 无 | 输出记录硬上限 |
| `--completion-token-cap` | 8192 | `max_out_len` 上限 |
| `--default-max-out-len` | 1024 | completion_tokens 缺失/0 时的回退值 |
| `--max-context` | 32768 | 引擎上下文预算 |
| `--context-reserve` | 1024 | 上下文预留 |
| `--exclude-compressor` | 关 | 丢弃 `:compressor:` 子会话 |
| `--report` | `<output>.report.json` | 报告路径 |

生成 finalize 变体：

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py \
  --input-dir /home/t00887700/benchmark-output/full50/on/model_io \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl \
  --state-mode completed-finalize
```

生成冒烟子集：

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py \
  --input-dir /home/t00887700/benchmark-output/full50/on/model_io \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_smoke_v2_running.jsonl \
  --session-limit 5
```

## 11. 已知限制

1. 这份数据来自 legacy body hint，不是真实的 v2 Header 抓包。
2. 没有 pause/resume、priority、cache_control、epoch、call_index、derivation。
3. 235 条 management/evict 请求被丢弃；如果后续需要测 management 语义，需要扩展 v2 数据 schema。
4. 1 条 compressor inference 因上下文预算被跳过。
5. finalize 是派生字段；running 版本是最保守、最忠实的数据集。
6. 数据文件在仓库外，配置文件使用绝对路径；移动数据后需要改配置。
7. `use_timestamp=True` 的全量回放约 4.5 小时，不适合快速迭代。
