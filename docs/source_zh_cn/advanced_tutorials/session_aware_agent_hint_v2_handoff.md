# Agent Hint V2（Header 协议）测评支持交接文档

## 1. 文档目的

本文用于交接「AISBench 支持 MindIE Motor 新版 Agent Hint（Header）协议性能测评」的分析结论、改动方案与验证计划。

目标场景：Motor 做多 vLLM 引擎路由调度，Agent Hint 通过 `X-Agent-Hint` HTTP Header 下发；需要对比 **Agent Hint 开启/关闭** 两种情况下的端到端性能，重点关注 **TTFT**。

结论先行：

- **可以测**，AISBench 只需把 Motor 当作一个 OpenAI 兼容 HTTP 入口，端到端 TTFT 口径不变。
- **不能直接复用当前 body 版 `agent_hint` 实现**，需要新增 Header 传输通道和 spec schema 的元数据提取。
- TTFT 收益主要来自 **Motor 会话亲和 + 引擎 prefix cache**；Motor 当前 `cache_control` 只实现 **affinity TTL（路由 pin 生命周期）**，不是引擎 KV 驻留保证。
- Header 内的 `agent_status` 当前不驱动 Motor 生命周期状态机；`finalize` 释放 pin 依赖带外 `POST /agent-status`。

## 2. 相关材料

| 材料 | 路径 |
|---|---|
| Motor 新协议定义 | `/home/t00887700/MindIE-Motor/docs/Agent Hint字段&实现定义.md` |
| Motor 推理侧设计 | `/home/t00887700/MindIE-Motor/docs/zh/design/agent_hint.md` |
| Motor 用户指南 | `/home/t00887700/MindIE-Motor/docs/zh/user_guide/features/agent_hint.md` |
| AISBench 旧版 Session/Hint 文档 | `origin/1030_session:docs/source_zh_cn/advanced_tutorials/session_aware_agent_hint.md` |

注意：当前 `dev` 分支没有旧版 `session_aware_agent_hint.md`，该文档只存在于 `origin/1030_session`。本文基于旧版文档、当前 `dev` 代码以及 Motor 侧实现整理。

## 3. 两版协议差异

### 3.1 当前 AISBench 实现

- `agent_hint` 作为请求体字段发送。
- 扁平字段：`session_id`、`parent_session_id`、`stage`、`cache_control`、`context_management`。
- 调度语义：
  - `stage="end"` 结束当前 session span；
  - `context_management.edits[].type="offload"` 触发 session 分裂；
  - `manage_request=true` 为控制面请求，不计入性能统计。
- `pass_agent_hint=True` 时把 `agent_hint` 合并进 vLLM 请求 body。
- 无自定义 HTTP Header 能力（仅 `Authorization`）。

### 3.2 Motor 新版协议

- `X-Agent-Hint` 为唯一 spec 载体，值为 **UTF-8 JSON 的 base64url 编码**，可省略 padding。
- 请求体里的 `agent_hint` 是 **冻结的 legacy 通道**，spec 字段只认 Header。
- Header JSON 主要结构：

```json
{
  "session": {
    "session_id": "s-7f3a2c",
    "parent_session_id": "s-root01",
    "derivation": { "relation": "spawn", "join": "sync" },
    "agent_type": "researcher",
    "call_index": 2,
    "epoch": 0
  },
  "agent_status": { "state": "running" },
  "priority": { "level": 3, "reason": "critical_path" },
  "strict_priority": { "level": 3, "reason": "critical_path" },
  "cache_control": { "type": "ephemeral", "ttl_s": 12, "range": { "end": 10 } },
  "prediction": {
    "remaining_calls": 3,
    "output_tokens": 420,
    "interval_ms": 11800,
    "latency_ms": 4100
  }
}
```

- 简化场景：

```json
{
  "session": {
    "session_id": "s-7f3a2c",
    "parent_session_id": "s-root01",
    "derivation": { "relation": "spawn", "join": "sync" },
    "epoch": 0
  },
  "agent_status": { "state": "pause|resume|finalize" },
  "priority": { "level": 3, "reason": "critical_path" },
  "cache_control": { "type": "ephemeral", "ttl_s": 12, "range": { "end": 10 } }
}
```

### 3.3 关键差异表

| 维度 | 当前 AISBench | Motor 新版 |
|---|---|---|
| 传输 | 请求体 `agent_hint` | `X-Agent-Hint` Header，base64url |
| session | 扁平 `agent_hint.session_id` | 嵌套 `session.session_id` / `parent_session_id` / `epoch` |
| 生命周期 | `stage=start/stage-N/end` + `context_management.edits` | `agent_status.state` + 带外 `/agent-status` |
| 缓存 | `cache_control` 占位 | `cache_control{type,ttl_s,range}`，pin/ephemeral |
| 优先级 | 无 | `priority` / `strict_priority` + `X-Strict-Priority-Token` |
| 调度键 | `parent_session_id` fallback `session_id` | `(role, session_id)`，parent 仅做亲和回退 |
| 控制面请求 | `manage_request=true` 排除统计 | 由带外 `/agent-status` 承担，Header `agent_status` 不驱动状态机 |

## 4. Motor 侧实现约束（影响测试设计）

1. **Body 通道冻结**
   - `apply_agent_hint_header()` 只把 Header 中可映射到 legacy 的字段合并进 body，且 **body 优先**。
   - 新协议测试时不要把 `agent_hint` 放进请求体，否则可能触发 body legacy 解析或绕过 strict_priority 信任门。

2. **Header `agent_status` 不驱动状态机**
   - Motor 会解析并存储 Header 里的 `agent_status`，但生命周期状态机与 `finalize` 释放 pin 由带外 `POST /agent-status` 驱动。
   - 如果测试只关注 TTFT、不关心 pin 释放，可先不实现带外通道，靠 `cache_control.ttl_s` / LRU 兜底。

3. **`cache_control` 是 affinity TTL，不是 KV retention**
   - Motor 当前只维护路由 pin 的保活/到期释放，不改变引擎 block 的保留/驱逐策略。
   - 因此 TTFT 收益主要来自「同会话固定到同一引擎 + 引擎自身 prefix cache」，而不是 `cache_control` 直接保留 KV。

4. **Header 大小限制**
   - Motor 解码前限制 16384 字符；AISBench 正常简化字段远低于该值。

5. **引擎侧不感知 Hint**
   - Header 不会透传给引擎，Motor 只在 Coordinator 调度层消费。

## 5. AISBench 需要做的改动

### 5.1 P0：跑通简化场景 TTFT 对比

#### 5.1.1 Header 传输能力

涉及文件：

- `ais_bench/benchmark/models/output.py`
- `ais_bench/benchmark/models/api_models/base_api.py`
- `ais_bench/benchmark/models/api_models/vllm_custom_api.py`

改动点：

1. `Output` 增加按请求保存的 Header：

```python
self.request_headers: dict = {}
```

2. `BaseApi.generate()` 合并静态 Header 与 per-request Header：

```python
headers = {**self.headers, **getattr(output, "request_headers", {})}
await self.stream_infer(request_body, output, headers=headers)
```

3. `stream_infer()` / `text_infer()` 接收 `headers` 参数，POST 时使用该参数，避免修改共享的 `self.headers`。

4. `VLLMCustomAPI` 新增配置：

```python
agent_hint_transport="header",   # "body" | "header" | "none"
extra_headers={"X-Strict-Priority-Token": "..."},  # 可选
```

5. `get_request_body()` 在 Header 模式下：

- 从 `args` 中 `pop("agent_hint")`，不写入 body；
- 序列化为 UTF-8 JSON；
- `base64.urlsafe_b64encode(...).rstrip(b"=")`；
- 写入 `output.request_headers["X-Agent-Hint"]`；
- 保留 `[agent_hint][send-header]` 日志便于排查。

编码示例：

```python
import base64
import json

raw = json.dumps(agent_hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
value = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
output.request_headers["X-Agent-Hint"] = value
```

#### 5.1.2 嵌套 session / 生命周期元数据提取

涉及文件：

- `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py`
- `ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py`

建议新增工具模块，例如 `ais_bench/benchmark/utils/agent_hint.py`：

```python
def session_id_of(hint: dict) -> str:
    session = hint.get("session")
    if isinstance(session, dict):
        return session.get("session_id") or ""
    return hint.get("session_id") or ""

def parent_session_id_of(hint: dict) -> str:
    session = hint.get("session")
    if isinstance(session, dict):
        return session.get("parent_session_id") or ""
    return hint.get("parent_session_id") or ""

def lifecycle_state_of(hint: dict) -> str:
    status = hint.get("agent_status")
    if isinstance(status, dict):
        return status.get("state") or ""
    return hint.get("stage") or ""
```

改动点：

1. `GenInferencer.get_data_list()`：
   - 从嵌套 `agent_hint.session.session_id` 提取 session_id；
   - 从 `agent_hint.agent_status.state` 提取内部生命周期状态；
   - v2 不再要求 `stage`，调整原有 start/end 校验逻辑，避免误告警。

2. `BaseApiInferencer` 调度器：
   - v2 分组键改为 `session.session_id`；
   - legacy 保持原 `parent_session_id -> session_id -> ""` 逻辑；
   - `_data_is_end()` 增加 `agent_status.state == "finalize"`；
   - 如简化场景包含 `pause`，增加 `_data_is_pause()`，将其视为一次 span 切分；
   - `resume` 不需要特殊处理，正常继续队列即可。

3. `do_request()`：
   - Header 模式下保留原始 `agent_hint` 供模型层编码 Header；
   - 不要把 spec `agent_hint` 写入 body；
   - `manage_request` 只属于 legacy body 协议，v2 不需要。

#### 5.1.3 新增配置与数据集

建议新增文件，避免影响旧配置：

- `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2.py`
- `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_hint_v2.py`

模型配置示例：

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
        extra_headers={},
        batch_size=4,
        request_rate=0,
        host_ip="motor-host",
        host_port=31036,
        max_out_len=256,
        generation_kwargs=dict(temperature=0, ignore_eos=False),
    )
]
```

数据集示例：

```python
from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

reader_cfg = dict(
    input_columns=["question", "max_out_len"],
    output_column=None,
)

infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{question}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

eval_cfg = dict(evaluator=None, pred_role="BOT")

datasets = [
    dict(
        abbr="agent-session-hint-v2",
        type=CustomDataset,
        path="ais_bench/datasets/agent/example_v2.jsonl",
        reader_cfg=reader_cfg,
        infer_cfg=infer_cfg,
        eval_cfg=eval_cfg,
    )
]
```

数据行示例：

```json
{
  "question": "You are a coding agent. Continue the task.",
  "max_out_len": 256,
  "agent_hint": {
    "session": {
      "session_id": "s-1",
      "parent_session_id": null,
      "derivation": {"relation": "spawn", "join": "sync"},
      "epoch": 0
    },
    "agent_status": {"state": "running"},
    "priority": {"level": 3, "reason": "critical_path"},
    "cache_control": {"type": "ephemeral", "ttl_s": 12, "range": {"end": 10}}
  }
}
```

#### 5.1.4 开/关对比方法

固定同一份数据、同一 `session_concurrency`、同一 warmup，只切换 `pass_agent_hint`：

```python
# ON
agent_hint_transport="header"
pass_agent_hint=True

# OFF
agent_hint_transport="header"
pass_agent_hint=False
```

OFF 时不发送 `X-Agent-Hint`，但 AISBench 仍按 session 调度。这样差异只来自「Motor 是否收到 Hint」，不会改变客户端请求节奏。

### 5.2 P1：完整生命周期 / 优先级

#### 5.2.1 带外 `/agent-status`

如果简化场景需要验证 `finalize` 释放 pin，需要新增 reporter：

```http
POST {base_url}/agent-status
Content-Type: application/json

{"session": {"session_id": "s-1"}, "agent_status": {"state": "finalize"}}
```

建议实现：

- 在 session worker 检测到 `finalize` 后异步发送；
- 不阻塞 LLM 请求、不进入性能统计；
- 失败只 WARN；
- 可选支持 `pause` / `resume` 状态事件。

#### 5.2.2 strict_priority

Motor 要求：

- `X-Agent-Hint` 内包含 `strict_priority`；
- 同时携带 `X-Strict-Priority-Token`，值命中 Motor 配置的 token。

AISBench 侧优先通过模型配置 `extra_headers` 静态注入即可。

#### 5.2.3 指标落盘

TTFT 计算已支持，无需改动：

- `default_perf.py` 中 `ttft = time_points[1] - time_points[0]`；
- 统计包含 Average / Min / Max / Median / P75 / P90 / P99。

建议在 `output.extra_perf_data` 追加以下字段，便于按会话/轮次分析：

- `parent_session_id`
- `lifecycle_state`
- `call_index`
- `epoch`
- `priority_level`
- `cache_control_type`
- `cache_control_ttl_s`

原始性能明细位于 `performances/**/tmp/*.jsonl`，可按 `session_id`、`call_index` 分组查看逐轮 TTFT。

## 6. 文件改动清单

| 文件 | 改动 |
|---|---|
| `ais_bench/benchmark/models/output.py` | 增加 `request_headers` |
| `ais_bench/benchmark/models/api_models/base_api.py` | per-request Header 支持；`extra_headers` 配置 |
| `ais_bench/benchmark/models/api_models/vllm_custom_api.py` | Header 模式 base64url 编码；body 排除 `agent_hint` |
| `ais_bench/benchmark/utils/agent_hint.py` | 新增 schema 提取工具（建议） |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py` | 嵌套 session/state 提取；v2 Header 透传 |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py` | session 分组键、finalize/pause 语义 |
| `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2.py` | 新增 Motor v2 模型配置 |
| `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_hint_v2.py` | 新增 v2 数据集配置 |
| `ais_bench/datasets/agent/example_v2.jsonl` | 新增最小样例数据 |
| 文档 / UT | 补充协议说明、Header 编码测试、调度测试 |

## 7. 验证计划

### 7.1 单测

- Header 编码：`base64url`、无 padding、UTF-8、空字段；
- Body 排除：Header 模式下请求体无 `agent_hint`；
- 嵌套 session 提取：`session.session_id` / `parent_session_id` / `epoch`；
- 生命周期：`finalize` 结束 span，`pause` 切分，`resume` 继续；
- `pass_agent_hint=False` 不发 Header，但 session 调度仍生效。

### 7.2 Motor 联调

先准备：

- Motor 已开启会话亲和：`MOTOR_SESSION_AFFINITY=1`，建议同时 `MOTOR_SESSION_AFFINITY_SHARED=1`、`MOTOR_SESSION_AFFINITY_PLACEMENT=balanced`；
- 需要 finalize 时开启 `agent_status_config.enabled=true`；
- 需要优先级时配置 `priority_reserve_ratio` / `priority_load_threshold` / `strict_priority_config`。

检查点：

1. Motor 日志出现 `aff=fresh → aff=hit`；
2. AISBench 日志出现 Header 发送记录；
3. Motor/引擎侧未在 body 中看到 spec `agent_hint`；
4. AISBench 性能报告输出 TTFT；
5. 同 session 多轮 TTFT 是否随轮次下降；
6. ON/OFF 两组 P90/P99 对比。

### 7.3 对比实验矩阵

| 组 | 拓扑 | Hint | 目的 |
|---|---|---|---|
| A | 单 vLLM 直连 | off | 基准 TTFT |
| B | 单 vLLM 直连 | on | 单引擎下 Hint 本身的影响 |
| C | Motor + 多引擎 | off | 基线路由（可能打散会话） |
| D | Motor + 多引擎 + 会话亲和 | on | 目标形态，验证 TTFT 收益 |
| E | Motor + 多引擎 + 会话亲和 + TTL/finalize | on | 验证 pin 生命周期 |

每组保持相同数据集、并发、warmup、引擎数量和模型权重；建议至少重复 3 轮，看 P90/P99，不只均值。

## 8. 预期结果与解读

- **Hint ON**：同 session 固定到同一引擎，后续调用命中引擎 prefix cache，TTFT 应下降。
- **Hint OFF**：Motor 按基线调度，同 session 请求可能散到不同引擎，TTFT 相对较高。
- **priority**：只有在服务接近过载或触发限流/软负载门槛时才会体现；空闲环境下基本无差异。
- **cache_control**：当前只影响 Motor 亲和 pin 的 TTL，不保证 KV 驻留；不要期待额外的 KV 保留收益。
- **agent_status/finalize**：主要影响 pin 释放与后续新会话放置；对单次请求 TTFT 不直接生效。
- **聚合 TTFT 会混合首轮和后续轮**：建议按 `session.session_id` 和 `call_index` 拆分原始明细，观察逐轮 TTFT，而不是只看全局平均。

## 9. 风险与待确认项

1. **简化场景是否包含带外状态事件**
   - 若只测 TTFT：P0 足够；
   - 若需验证 `pause/resume/finalize` 对 pin 生命周期的影响：必须实现 `/agent-status` reporter。

2. **Motor 版本差异**
   - 不同版本对 Header 大小、字段白名单、`cache_control.range.unit` 的处理可能不同；
   - 联调前确认 Motor 分支与文档版本一致。

3. **多引擎 prefix cache 非共享**
   - 若路由未命中亲和，同会话请求落到不同引擎，TTFT 不会下降；
   - 先验证 `aff=hit`，再比较 TTFT。

4. **warmup / 冷缓存影响**
   - 两组必须使用相同 warmup 策略；
   - 第一轮通常是冷缓存，建议单独统计或丢弃。

5. **非流式无 TTFT**
   - 必须保持 `stream=True`。

6. **AISBench 当前分支缺少旧版文档**
   - 旧版 `session_aware_agent_hint.md` 在 `origin/1030_session`；
   - 若后续实现依赖旧代码，先确认基线分支。

## 10. 交接 TODO

- [ ] 确认简化场景是否包含 `/agent-status` 带外事件；
- [ ] 确认 Motor 目标版本与 Header/schema 行为；
- [ ] 实现 `Output.request_headers` 与 `BaseApi` per-request Header；
- [ ] 实现 `VLLMCustomAPI` Header 模式与 base64url 编码；
- [ ] 实现嵌套 session / 生命周期元数据提取；
- [ ] 适配 session 调度分组与 finalize/pause 语义；
- [ ] 新增 v2 模型/数据集配置与样例 JSONL；
- [ ] 增加 Header 编码、body 排除、调度语义单测；
- [ ] 完成 Motor 联调与 ON/OFF TTFT 对比；
- [x] 确认简化场景包含 `pause` / `resume`；
- [x] 实现 `Output.request_headers` 与 `BaseApi` per-request Header；
- [x] 实现 `VLLMCustomAPI` Header 模式与 base64url 编码；
- [x] 实现嵌套 session / 生命周期元数据提取；
- [x] 适配 session 调度分组与 finalize/pause/resume 语义；
- [x] 新增 v2 模型/数据集配置与样例 JSONL；
- [x] 增加 Header 编码、body 排除、调度语义与 `/agent-status` reporter 单测；
- [x] 完成 NPU + Motor 联调（单引擎、ON/OFF、pause/resume/finalize）；
- [ ] 补充用户文档与结果解读说明。

## 11. 实现说明（2026-11-05）

### 11.1 新增配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| `agent_hint_transport` | `"body"` | `body` / `header` / `none`；`header` 时编码为 `X-Agent-Hint`，且不写入请求体 |
| `extra_headers` | `{}` | 静态附加请求头，如 `X-Strict-Priority-Token` |
| `agent_status_report` | `False` | 是否把生命周期事件上报到 `/agent-status` |
| `agent_status_url` | `""` | 默认 `base_url + "agent-status"`，可传相对路径或完整 URL |

### 11.2 生命周期约定

- `agent_status.state="pause"` / `"suspend"`：请求完成后上报对应事件，并切分 session span。
- `agent_status.state="resume"`：AISBench 数据集侧别名；Header 中归一化为 `running`，并在请求前通过 `/agent-status` 上报 `running`，与 Motor 的“恢复为 running”语义一致。
- `agent_status.state="finalize"`：请求完成后上报 `finalize`，结束 session span。
- `agent_status.state="running"` / `"tool"`：`tool` 会异步上报事件但不切分 span；`running` 不额外上报。

### 11.3 验证结果

- 单测：`tests/UT/utils/test_agent_hint.py`、`tests/UT/openicl/icl_inferencer/test_agent_hint_scheduling.py` 等共 130 项通过。
- NPU + Motor 联调：
  - AISBench 日志出现 `[agent_hint][send-header]`；
  - Motor `scheduled` 日志出现 `prio=3`、`aff=fresh → aff=hit`；
  - Motor `agent_status event` 对 `pause` / `running`（resume）/ `finalize` 均 `applied=True`；
  - AISBench 输出 TTFT，ON/OFF 对比可正常落盘。
- 环境注意：部分开发环境同时加载 `av` 与 `torch` 时，任务状态线程启动可能触发段错误；本次联调通过移除 `av` 规避。该问题与 Agent Hint 逻辑无关，建议后续在 dev 分支评估是否复用 `origin/1030_session` 的 lazy task re-export 方案。

### 11.4 真实流量转换

输入为 JiuwenSwarm 的 SWE-bench 抓包：

```text
/home/t00887700/benchmark-output/full50/on/model_io/*.json
567 条记录：332 条 inference + 235 条 management
```

转换脚本：

```shell
python3 ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py \
  --input-dir /home/t00887700/benchmark-output/full50/on/model_io \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_running.jsonl
```

已生成：

| 文件 | 记录数 | session 数 | 说明 |
|---|---:|---:|---|
| `swebench_full50_v2_running.jsonl` | 331 | 58 | 只保留抓包可证明的 `running` |
| `swebench_full50_v2_finalize.jsonl` | 331 | 58 | 额外派生 51 个 `finalize` |
| `swebench_smoke_v2_running.jsonl` | 13 | 5 | 冒烟子集 |
| `swebench_smoke_v2_finalize.jsonl` | 13 | 5 | 冒烟子集 + 派生 finalize |

准确性约束：

- `session_id` / `parent_session_id` 直接来自 `request.agent_hint`；
- `agent_status.state=running` 是抓包可支持的唯一状态；默认不派生 finalize；
- `completed-finalize` 模式只对“最后一条 inference 且抓包状态为 completed”的 session 派生 finalize，报告中明确标记为 derived；
- `priority` / `cache_control` / `epoch` / `call_index` / `derivation` 抓包中没有，**不合成**；
- `turn_num` 在抓包中恒为 1，不作为 `call_index`；
- 235 条 management/evict 请求被丢弃（v2 Header 无 `manage_request` 对应物）；
- 1 条 inference 因输入超出 32k 上下文预算被跳过；
- `messages` / `tools` 以 JSON 字符串存储，避免 Arrow struct 补 `None`；模型配置通过 `json_extra_fields=["messages","tools"]` 解析回对象；
- `max_out_len` 取抓包 `completion_tokens`，按 `--max-context` 和 `--completion-token-cap` 截断。

对应配置：

```text
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_finalize.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_smoke.py
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_smoke_finalize.py
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_trace.py
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_trace_off.py
```
