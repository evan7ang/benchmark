# Agent Hint V2（Header 协议）性能与功能测试教程

本文介绍如何使用 AISBench 对 MindIE Motor 新版 Agent Hint（`X-Agent-Hint` Header 协议）进行**性能测试**和**协议功能测试**。适用于需要对比 Agent Hint 开启/关闭后端到端性能、验证会话亲和、pause/resume/finalize 生命周期的场景。

## 1. 能力边界

### 1.1 支持的能力

+ ✅ 服务化性能测试：`--mode perf`
+ ✅ 协议功能验证：Header 透传、session 调度、pause/resume/finalize、`/agent-status`
+ ✅ ON/OFF 对比：同一数据集只切换 `pass_agent_hint`
+ ✅ 多 session 并发、同 session 串行
+ ✅ strict_priority 静态请求头注入

### 1.2 不支持或需额外开发的能力

+ ❌ 不是精度评测基准：v2 样例数据集 `evaluator=None`，默认不计算准确率
+ ❌ 不自动生成 Hint：`session_id`、`priority`、`cache_control` 等必须由数据集提供
+ ❌ 不支持把 v2 spec 写进请求体：请求体 `agent_hint` 是冻结的 legacy 通道
+ ❌ 单引擎不能验证会话亲和带来的 TTFT 收益：需要多引擎 + 会话亲和

### 1.3 与旧版 Body 协议的区别

| 维度 | 旧版 Body 协议 | V2 Header 协议 |
|---|---|---|
| 传输通道 | 请求体 `agent_hint` | `X-Agent-Hint` Header，base64url JSON |
| session 字段 | 扁平 `session_id` | 嵌套 `session.session_id` |
| 生命周期 | `stage=start/stage-N/end` | `agent_status.state=running/pause/resume/finalize` |
| 是否透传引擎 | 是 | 否，Motor 调度层消费 |
| 控制面请求 | `manage_request=true` | `/agent-status` 带外事件 |

## 2. 前置条件

### 2.1 软件与硬件

+ AISBench：已包含本教程对应实现的 `dev` 分支
+ Motor：支持 Agent Hint Header 协议的版本（`/home/t00887700/agent-hint/MindIE-Motor`）
+ 引擎：vLLM / vLLM-Ascend，必须支持流式接口
+ 硬件：至少 1 张空闲 NPU/GPU；验证亲和收益建议至少 2 个引擎实例

### 2.2 Motor 配置

`coordinator.json` 至少包含：

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "127.0.0.1"
    },
    "agent_status_config": {
      "enabled": true,
      "session_ttl_s": 3600,
      "max_sessions": 65536,
      "require_infer_auth": false
    }
  }
}
```

> `agent_status_config.enabled` 在 Coordinator 启动时生效，修改后需重启 Coordinator。
>
> 容器部署时将 `coordinator_api_host` 设为 `0.0.0.0`，并通过宿主机端口映射访问推理端口（standalone 默认 `1025`）。

启动 Coordinator 时建议开启会话亲和：

```shell
export MOTOR_SESSION_AFFINITY=1
export MOTOR_SESSION_AFFINITY_SHARED=1
export MOTOR_SESSION_AFFINITY_PLACEMENT=balanced
export USER_CONFIG_PATH=/path/to/coordinator.json
python3 -m motor.coordinator.main
```

### 2.3 引擎要求

+ 使用 `v1/chat/completions` 或 `v1/completions`
+ 流式：`stream=True`
+ 建议开启 prefix cache
+ 单实例显存/内存需能容纳模型

## 3. 协议与数据格式

### 3.1 Header 编码

`X-Agent-Hint` 的值是 UTF-8 JSON 的 **base64url** 编码，可省略 `=` padding。AISBench 会自动完成编码，无需手工处理。

### 3.2 推荐数据结构

```json
{
  "session": {
    "session_id": "s-7f3a2c",
    "parent_session_id": "s-root01",
    "derivation": {"relation": "spawn", "join": "sync"},
    "agent_type": "researcher",
    "call_index": 2,
    "epoch": 0
  },
  "agent_status": {"state": "running"},
  "priority": {"level": 3, "reason": "critical_path"},
  "strict_priority": {"level": 3, "reason": "critical_path"},
  "cache_control": {
    "type": "ephemeral",
    "ttl_s": 12,
    "range": {"end": 10}
  }
}
```

### 3.3 生命周期状态

Motor 接受的状态：

```text
running | tool | pause | suspend | finalize
```

AISBench 数据集可使用 `resume` 作为别名：

+ `resume` 在 Header 中自动归一化为 `running`
+ 同时在请求前通过 `/agent-status` 上报 `running`
+ 适用于“pause 后恢复”的场景

### 3.4 数据集示例

每行一条 JSON，至少包含 `question`、`max_out_len`、`agent_hint`：

```json
{"question":"Continue the task.","max_out_len":256,"agent_hint":{"session":{"session_id":"s-1","parent_session_id":null,"derivation":{"relation":"spawn","join":"sync"},"epoch":0},"agent_status":{"state":"running"},"priority":{"level":3,"reason":"critical_path"},"cache_control":{"type":"ephemeral","ttl_s":600,"range":{"end":10}}}}
{"question":"Pause after this turn.","max_out_len":256,"agent_hint":{"session":{"session_id":"s-1","parent_session_id":null,"derivation":{"relation":"spawn","join":"sync"},"epoch":0},"agent_status":{"state":"pause"},"priority":{"level":3,"reason":"critical_path"},"cache_control":{"type":"ephemeral","ttl_s":600,"range":{"end":10}}}}
{"question":"Resume the task.","max_out_len":256,"agent_hint":{"session":{"session_id":"s-1","parent_session_id":null,"derivation":{"relation":"spawn","join":"sync"},"epoch":0},"agent_status":{"state":"resume"},"priority":{"level":3,"reason":"critical_path"},"cache_control":{"type":"ephemeral","ttl_s":600,"range":{"end":10}}}}
{"question":"Finish the task.","max_out_len":256,"agent_hint":{"session":{"session_id":"s-1","parent_session_id":null,"derivation":{"relation":"spawn","join":"sync"},"epoch":0},"agent_status":{"state":"finalize"},"priority":{"level":3,"reason":"critical_path"},"cache_control":{"type":"ephemeral","ttl_s":600,"range":{"end":10}}}}
```

仓库自带最小样例：

```text
ais_bench/datasets/agent/example_v2.jsonl
```

> ⚠️ 注意：v2 测试不要把 spec 字段写入请求体 `agent_hint`。Motor 的 body 通道是冻结的 legacy 通道，可能触发旧解析或绕过 strict_priority 信任门。

### 3.5 cache_control 语义

当前 Motor 的 `cache_control` 是 **affinity TTL**，只影响路由 pin 的生命周期，不保证引擎 KV 驻留。TTFT 收益主要来自：

1. 同 session 固定到同一引擎；
2. 引擎自身 prefix cache 命中。

## 4. 配置文件说明

### 4.1 模型配置

文件：

```text
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2.py
```

关键字段：

| 字段 | 说明 |
|---|---|
| `agent_hint_transport` | `body` / `header` / `none`；v2 使用 `header` |
| `pass_agent_hint` | `True` 发送 Hint；`False` 只在本地用于调度，不发送 |
| `agent_status_report` | `True` 时上报 `/agent-status` |
| `agent_status_url` | 默认 `base_url + "agent-status"`，可覆盖 |
| `extra_headers` | 静态请求头，如 `X-Strict-Priority-Token` |
| `session_aware` | 必须为 `True` |
| `session_concurrency` | 同时活跃的 session 数上限 |
| `stream` | 必须为 `True`，否则没有 TTFT |
| `host_ip` / `host_port` | 指向 Motor 推理入口，standalone 默认端口 `1025` |

示例：

```python
from ais_bench.benchmark.models import VLLMCustomAPI

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-motor-agent-hint-v2",
        path="/path/to/tokenizer",
        model="qwen",
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=4,
        agent_hint_transport="header",
        pass_agent_hint=True,
        agent_status_report=True,
        extra_headers={},
        batch_size=4,
        request_rate=0,
        host_ip="127.0.0.1",
        host_port=1025,
        max_out_len=256,
        generation_kwargs=dict(temperature=0, ignore_eos=False),
    )
]
```

### 4.2 数据集配置

文件：

```text
ais_bench/benchmark/configs/datasets/custom/custom_agent_session_hint_v2.py
```

默认指向：

```text
ais_bench/datasets/agent/example_v2.jsonl
```

使用自定义数据时修改 `path` 即可。

### 4.3 ON / OFF 开关

| 模式 | `pass_agent_hint` | 发送 Header | 发送 `/agent-status` | 本地 session 调度 |
|---|---|---|---|---|
| ON | `True` | 是 | 是（`agent_status_report=True`） | 是 |
| OFF | `False` | 否 | 否 | 是 |

ON/OFF 实验只切换 `pass_agent_hint`，保持数据、并发、warmup 完全一致。

## 5. 快速开始（单引擎）

### 5.1 启动 vLLM

以 vLLM-Ascend 为例：

```shell
export ASCEND_RT_VISIBLE_DEVICES=0
export VLLM_ASCEND_ENABLE_AGENT_HINT=1
vllm serve /path/to/Qwen3-8B \
  --served-model-name qwen3-8b \
  --host 0.0.0.0 --port 18000 \
  --max-model-len 8192 \
  --enable-prefix-caching \
  --enforce-eager
```

### 5.2 启动 Motor 并注册引擎

```shell
python3 -m motor.coordinator.main
```

注册单引擎（PD 同实例，使用 `concurrent_engine_sync`）：

```shell
curl -X POST http://127.0.0.1:1026/instances/refresh \
  -H 'Content-Type: application/json' \
  -d '{
    "event": "set",
    "dispatch_capabilities": "concurrent_engine_sync",
    "instances": [
      {"id": 1, "role": "prefill", "endpoints": [{"id": 0, "address": "127.0.0.1:18000"}]},
      {"id": 2, "role": "decode",  "endpoints": [{"id": 0, "address": "127.0.0.1:18000"}]}
    ]
  }'
```

检查就绪：

```shell
curl http://127.0.0.1:1026/readiness
```

> 上面是 PD 混部（同一引擎同时承担 P/D）的注册方式。若使用 PD 分离部署，将 `dispatch_capabilities` 改为 `prefill_handoff_decode`，并分别注册不同的 prefill / decode 引擎地址。

### 5.3 修改 AISBench 配置

修改模型配置中的：

+ `path`：tokenizer 或模型路径，需存在
+ `model`：引擎 `served-model-name`
+ `host_ip` / `host_port`：Motor 推理入口
+ `session_concurrency`：按显存和 session 数调整
+ `agent_status_report=True`
+ `pass_agent_hint=True`

数据集配置中的 `path` 指向自己的 JSONL。

### 5.4 运行 ON

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2 \
          --datasets custom_agent_session_hint_v2 \
          --mode perf
```

### 5.5 运行 OFF

复制一份模型配置，例如：

```text
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_off.py
```

把 `pass_agent_hint` 改为 `False`，其余保持不变，然后运行：

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2_off \
          --datasets custom_agent_session_hint_v2 \
          --mode perf
```

### 5.6 预期日志

AISBench ON：

```text
[agent_hint][send-header] data_id=0 session_id=s-1 state=running header_len=...
[agent_hint][agent-status] session_id=s-1 state=pause status=200 ...
[agent_hint][agent-status] session_id=s-1 state=running status=200 ...
[agent_hint][agent-status] session_id=s-1 state=finalize status=200 ...
[session-aware] session s-1 START request stage=running ...
```

Motor ON：

```text
scheduled role=decode ... prio=3 aff=fresh reason=critical_path
scheduled role=decode ... prio=3 aff=hit   reason=critical_path
agent_status event session_id=s-1 state=pause applied=True ...
agent_status event session_id=s-1 state=running applied=True previous=pause
agent_status event session_id=s-1 state=finalize applied=True ...
```

Motor OFF：

```text
scheduled role=decode ... prio=None aff=- reason=None
```

## 6. 性能测试流程

### 6.1 实验原则

+ 同一份数据集
+ 同一 `session_concurrency`
+ 同一 warmup 次数
+ 同一引擎数量、模型权重、`max_out_len`
+ ON/OFF 只切换 `pass_agent_hint`
+ 建议至少重复 3 次，关注 P90/P99，不只看均值

### 6.2 运行命令

```shell
# ON
ais_bench --models vllm_api_general_stream_session_hint_v2 \
          --datasets custom_agent_session_hint_v2 \
          --mode perf --debug

# OFF
ais_bench --models vllm_api_general_stream_session_hint_v2_off \
          --datasets custom_agent_session_hint_v2 \
          --mode perf --debug
```

### 6.3 关注指标

| 指标 | 说明 |
|---|---|
| TTFT | 首 token 延迟，核心指标 |
| TPOT / ITL | 每 token 延迟 / token 间延迟 |
| E2EL | 端到端延迟 |
| OutputTokenThroughput | 输出吞吐 |
| Concurrency / Max Concurrency | 实际并发 / 峰值并发 |

结果文件：

```text
outputs/**/performances/**/*.json
outputs/**/performances/**/*_details.jsonl
```

### 6.4 逐轮 TTFT

聚合 TTFT 会混合首轮和后续轮。建议从 `*_details.jsonl` 按 `session_id`、`call_index` 分组：

```python
import json
from collections import defaultdict

groups = defaultdict(list)
for line in open("..._details.jsonl"):
    row = json.loads(line)
    meta = row.get("extra_perf_data", {})
    key = (row.get("session_id"), meta.get("call_index"))
    groups[key].append(row)
```

观察同 session 后续轮次 TTFT 是否下降。

### 6.5 实验矩阵

| 组 | 拓扑 | Hint | 目的 |
|---|---|---|---|
| A | 单 vLLM 直连 | off | 基准 TTFT |
| B | 单 vLLM 直连 | on | 单引擎下 Hint 本身的影响 |
| C | Motor + 多引擎 | off | 基线路由，可能打散会话 |
| D | Motor + 多引擎 + 会话亲和 | on | 目标形态，验证 TTFT 收益 |
| E | Motor + 多引擎 + 会话亲和 + TTL/finalize | on | 验证 pin 生命周期 |

> 单引擎 A/B 组不能体现会话亲和的 TTFT 收益，属于正常现象。

### 6.6 多引擎亲和检查

多引擎场景下，先看 Motor 日志：

```text
aff=fresh  # 新会话放置
aff=hit    # 同 session 后续请求命中同一引擎
```

只有 `aff=hit` 稳定出现，再比较 ON/OFF 的 TTFT。

## 7. 功能测试流程

### 7.1 单元测试

```shell
python3 -m pytest -q \
  tests/UT/utils/test_agent_hint.py \
  tests/UT/openicl/icl_inferencer/test_agent_hint_scheduling.py \
  tests/UT/models/api_models/test_vllm_custom_api.py \
  tests/UT/models/api_models/test_base_api.py \
  tests/UT/openicl/icl_inferencer/test_icl_gen_inferencer.py
```

覆盖点：

+ Header base64url 编码、无 padding、UTF-8
+ Header 模式下 body 不含 `agent_hint`
+ 嵌套 `session.session_id` / `parent_session_id` / `epoch`
+ `pause` / `suspend` 切分 span
+ `resume` 继续并上报 `running`
+ `finalize` 结束 span
+ `pass_agent_hint=False` 不发 Header、不发 `/agent-status`
+ `extra_headers` 与 `agent_status_url`

### 7.2 端到端功能验证

准备一个包含以下生命周期的 session：

```text
running -> pause -> resume -> finalize
```

运行：

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2 \
          --datasets custom_agent_session_hint_v2 \
          --mode perf --debug
```

检查清单：

- [ ] AISBench 日志出现 `[agent_hint][send-header]`
- [ ] AISBench 日志出现 pause/resume/finalize 的 `[agent_hint][agent-status]`
- [ ] Motor 日志出现 `agent_status event ... applied=True`
- [ ] Motor 日志出现 `aff=fresh -> aff=hit`
- [ ] Motor 日志出现 `prio=3`、`sp=`（如配置 strict_priority）
- [ ] 请求体中没有 legacy `agent_hint`（可用抓包/反向代理确认，或参考单测 `test_agent_hint_header_transport`）
- [ ] OFF 运行无 Header、无 status，Motor 为 `prio=None aff=-`
- [ ] OFF 运行仍按 session 串行（AISBench `[session-aware]` 日志不变）
- [ ] 性能报告包含 TTFT

### 7.3 `/agent-status` 验证

上报：

```shell
curl -X POST http://<motor-infer-ip>:<port>/agent-status \
  -H 'Content-Type: application/json' \
  -d '{"session":{"session_id":"s-1","epoch":0},"agent_status":{"state":"pause"}}'
```

查询：

```shell
curl "http://<motor-infer-ip>:<port>/agent-status?session_id=s-1"
```

预期：

```json
{"status":"ok","applied":true,"reason":"applied","state":"pause","previous_state":null}
```

> ⚠️ Motor 的 `GET /agent-status` 只查询当前 worker 的记录；多 worker 下查询可能 404，这是设计限制。

### 7.4 功能测试矩阵

| 场景 | 操作 | 预期 |
|---|---|---|
| 单 session 多轮 | 同 `session_id` 连续请求 | 同 session 严格串行，`aff=hit` |
| 多 session 并发 | 多个 `session_id` | 跨 session 并发，`Max Concurrency <= session_concurrency` |
| pause | 请求携带 `state=pause` | 当前 span 结束，上报 pause |
| resume | 请求携带 `state=resume` | 上报 running，继续同 session |
| finalize | 请求携带 `state=finalize` | 上报 finalize，释放 pin |
| OFF 对照 | `pass_agent_hint=False` | 无 Header/status，仍有 session 调度 |
| strict_priority | `extra_headers` 注入 token | Motor `sp=` 非空 |
| 多引擎 | 同 session 多轮 | `aff=fresh -> aff=hit`，TTFT 随轮次下降 |

## 8. 结果解读

+ **ON 优于 OFF**：同 session 固定到同一引擎，后续轮命中 prefix cache，TTFT 下降。
+ **ON/OFF 接近**：可能是单引擎、路由未命中亲和、数据集 prefix 重复度低、或 warmup 不充分。
+ **priority 无差异**：空闲环境下 priority 不生效，接近过载时才体现。
+ **cache_control 无额外 KV 收益**：当前只影响 affinity pin TTL。
+ **finalize 不改变单次 TTFT**：它影响 pin 释放和后续新会话放置。

## 9. 常见问题

### Q1：ON 时 AISBench 会自动补充 hint 吗？
不会。AISBench 只转发数据集里的 `agent_hint`。数据集没有该字段时，ON 也不会发送。

### Q2：OFF 时还会按 session 调度吗？
会。`pass_agent_hint=False` 只是不把 Hint 发给服务端，AISBench 仍读取 `agent_hint` 做本地 session 串行、pause 切分和 finalize 结束。

### Q3：`resume` 为什么在 Motor 里看不到？
Motor 的生命周期状态不包含 `resume`，恢复语义是 `running`。AISBench 数据集可以写 `resume`，发送前会归一化为 `running`，并带外上报 `running`。

### Q4：重复运行同一 `session_id`，pause/resume 被忽略？
`finalize` 是终态。重复使用同一 session 时，后续 pause/running 会返回 `applied=false reason=terminal`。建议：
- 每轮测试重启 Coordinator；或
- 使用新的 `session_id`；或
- 使用更大的 `epoch` 触发 `resumed_epoch`。

### Q5：GET `/agent-status` 返回 404？
多 worker 下 GET 只查询本进程记录，POST 可能落在其他 worker。属于正常现象。

### Q6：请求体里还需要写 `agent_hint` 吗？
不需要，v2 只走 Header。body 的 `agent_hint` 是冻结 legacy 通道。

### Q7：为什么没有 TTFT？
必须 `stream=True`。非流式请求 TTFT 会被置 0。

### Q8：`tools: []` 导致 400？
vLLM 要求 `tools` 要么省略，要么至少包含一个工具。不需要工具时不要写空数组。

### Q9：`cache_control: {}` 在部分数据里变成 `{"type": null, "ttl_s": null}`？
HuggingFace datasets 会按列 schema 推断嵌套结构，混合有值/空字典时会补 `None`。AISBench 发送 Header 前会清理 `None` 和空字典；仍建议同一数据集保持 `cache_control` 结构一致。

### Q10：debug 模式启动任务线程段错误？
部分环境同时加载 `av` 与 `torch` 时会触发段错误，与 Agent Hint 逻辑无关。可临时移除 `av` 规避，或后续复用 lazy task re-export 方案。

### Q11：Header 长度限制？
Motor 解码前限制 16384 字符。AISBench 超过该长度会告警。简化字段通常远低于限制。

### Q12：`max_out_len` 和 `max_tokens` 哪个生效？
+ 数据集有 `max_out_len` 且在 `input_columns` 中：使用数据集值；
+ 数据集只有 `max_tokens`：作为额外字段透传到 vLLM，覆盖模型配置；
+ 两者都没有：使用模型配置的 `max_out_len`。

## 10. 参考文件

| 文件 | 作用 |
|---|---|
| `ais_bench/benchmark/utils/agent_hint.py` | v2 字段提取、Header 编码、resume 归一化 |
| `ais_bench/benchmark/models/output.py` | per-request Header 存储 |
| `ais_bench/benchmark/models/api_models/base_api.py` | Header 合并、`/agent-status` 上报 |
| `ais_bench/benchmark/models/api_models/vllm_custom_api.py` | Header 传输、body 排除 |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py` | 嵌套 session/lifecycle 提取、perf 元数据 |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py` | session 分组、pause/finalize/resume 语义、reporter |
| `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2.py` | v2 模型配置 |
| `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_hint_v2.py` | v2 数据集配置 |
| `ais_bench/datasets/agent/example_v2.jsonl` | 最小样例数据 |
| `tests/UT/utils/test_agent_hint.py` | Agent Hint 工具单测 |
| `tests/UT/openicl/icl_inferencer/test_agent_hint_scheduling.py` | 调度/reporter 单测 |
