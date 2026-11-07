# Agent Hint V2 KVC Pause/Offload 真实档压测数据与 AISBench 支持需求

> 面向 `/home/t00887700/benchmark` 的实现需求。
> 目标不是再测多引擎亲和，而是**单实例、压满 KV、测 KVC 管理（pause → offload → resume → prefetch，finalize → evict）的收益**。

## 1. 背景与目标

现成数据集 `swebench_full50_v2_*.jsonl` 只有 `running` / `finalize`，能触发 finalize→evict，但不能触发 pause→offload→resume→prefetch。之前的 A/B 在 3 engine、c=8 下 KV usage 仅约 10%，没有压力，KVC evict 没有容量收益。

本需求要补齐：

1. 一份可触发 **offload/prefetch** 的 pause/resume 数据集；
2. AISBench 的 **inter-request delay** 能力（pause 后必须等够 idle 时间，否则 resume 会先取消 timer，offload 不会发生）；
3. 单实例 TP1 下可复现的压测与 KVC A/B 方案。

## 2. 已确认决策

| 项 | 决策 |
|---|---|
| 拓扑 | **单实例 TP1**（1 张卡） |
| KVC 模式 | pause → offload；resume → prefetch；finalize → evict |
| idle/offload 档位 | **真实档：`KVC_IDLE_OFFLOAD_S=30`，pause 后 delay ≥ 40s** |
| finalize | **保留**，仍用于 finalize evict |
| 协议 | v2 Header + `/agent-status`，不改 v2 schema |
| 数据构造 | 基于现有 v2 finalize 数据集做后处理，或扩展现有 builder |
| 多引擎 | 本需求不测；亲和收益另行验证 |

## 3. 现状与缺口

### 3.1 已有能力

- `ais_bench/datasets/agent/build_agent_dataset_v2_from_model_io.py`：
  - 支持 `--state-mode running` / `completed-finalize`；
  - 产出 `session_id / parent_session_id / agent_status / messages / tools / max_out_len / timestamp`。
- `ais_bench/datasets/agent/example_v2.jsonl`：
  - 已验证 `running → pause → resume → finalize` 的字段写法。
- `icl_base_api_inferencer.py` session-aware worker：
  - `resume` 请求前 POST `running`；
  - `pause/suspend` 请求后 POST `pause` 并切分 span；
  - `finalize` 请求后 POST `finalize`；
  - `tool` 请求后 POST `tool`，但不切分 span。
- `icl_gen_inferencer.py`：
  - 会 pop `session_id / timestamp / stage / agent_hint` 等控制字段。

### 3.2 缺口

1. **session-aware 路径没有 inter-request delay**：pause POST 后立即进入下一行 resume，`running` 会取消 Motor idle timer，offload 不会触发。
2. **`timestamp` 在 session-aware 路径不参与调度**：不能靠现有 timestamp 字段实现 pause 等待。
3. 现有 `build_agent_dataset.py::add_pause_in_session` 是 legacy body `manage_request` 的 offload/prefetch，不是 v2 Header 的 pause/resume，**不能直接复用**。
4. 当前 builder 的 `max_out_len` 用抓包 `usage.input_tokens` 估算，和 AISBench 实际 chat template token 数有偏差，已知会出现 `prompt 32257 + max_out 512 = 32769 > 32768` 的 400。新数据必须做 tokenizer 级校验。

## 4. 数据需求

### 4.1 输入与输出

推荐输入：

```text
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl
```

推荐输出：

```text
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl
```

实现方式二选一：

- **推荐**：新增后处理脚本 `ais_bench/datasets/agent/build_agent_dataset_v2_pause.py`，直接消费已审计过的 v2 finalize JSONL；
- 或在 `build_agent_dataset_v2_from_model_io.py` 增加 `--state-mode pause-offload` 模式。

两种方式输出字段必须完全一致。

### 4.2 生成规则

对每个 session 按行顺序处理：

1. 只对 **能容纳 pause → resume → finalize 的 session** 插入 pause：`请求数 ≥ N+2`（默认 `N=3` 时即 ≥5）；单请求/过短 session 不插，并在 report 记录原因。
2. 每 `N` 个 turn 选一个 pause 点（默认 `N=3`，可配置）。这里的 turn 按 **1-based** 计，即第 N、2N… 个请求。候选点的下一行必须是同一 session 的非最后一行；否则跳过（避免把最后一行改成 resume，或 pause 后直接 finalize）。
3. 当前行：
   - `agent_hint.agent_status.state = "pause"`；
   - 新增 `delay_after_ms = 40000`（真实档；要求 `delay_after_ms > idle_offload_s*1000 + 5000`）；
   - 可选加入 0–5000ms jitter，避免所有 session 同时 resume。
4. 下一行：
   - `agent_hint.agent_status.state = "resume"`；
   - 不设置 `delay_after_ms`；
   - 跳过该行，避免连续 pause。
5. 最后一行保持原状态；**不把最后一行改成 resume**，**如果下一行是 finalize，则当前行不插 pause**。输入中本来未以 finalize 结尾的 session 保留原样并在 report 记为 `unfinished_sessions`，**不得伪造 finalize**。
6. `messages` / `tools` / `session_id` / `parent_session_id` / `max_out_len` 保持不变（`max_out_len` 仅允许因上下文校验而下调，并在 report 记录）。
7. **不添加 `cache_control`**：它只影响 affinity pin TTL，对单实例 KVC 无收益，还可能 pin 住 session 干扰 finalize/evict；builder 不合成抓包中不存在的字段。

### 4.3 行格式示例

pause 行：

```json
{
  "question": "django__django-11087",
  "max_out_len": 512,
  "delay_after_ms": 40000,
  "messages": "<原 messages JSON 字符串>",
  "tools": "<原 tools JSON 字符串>",
  "agent_hint": {
    "session": {
      "session_id": "bench_...",
      "parent_session_id": "bench_..."
    },
    "agent_status": {"state": "pause"}
  }
}
```

resume 行：

```json
{
  "question": "django__django-11087",
  "max_out_len": 512,
  "messages": "<原 messages JSON 字符串>",
  "tools": "<原 tools JSON 字符串>",
  "agent_hint": {
    "session": {
      "session_id": "bench_...",
      "parent_session_id": "bench_..."
    },
    "agent_status": {"state": "resume"}
  }
}
```

finalize 行保持原样：

```json
"agent_status": {"state": "finalize"}
```

### 4.4 上下文校验（必须）

生成后必须用目标 tokenizer（`/home/RAG_data/models/Qwen3-8B`）校验每个请求：

```text
prompt_tokens(messages + tools + chat template) + max_out_len <= 32768
```

不满足的行必须：

- 下调 `max_out_len`，或
- 直接丢弃并在 report 中记录。

若 `prompt_tokens` 本身已 `>= 32768`，无法通过下调 `max_out_len` 修复，必须丢弃。上下文校验应在插入 pause 之前或之后保持 session 生命周期自洽：丢弃行后不能留下孤立的 pause/resume。

不能再用抓包 `usage.input_tokens` 直接推算，否则会重现 32769 的 400。注意 `messages` + `tools` 的 chat template 渲染可能比不带 tools 的 prompt 大很多，必须带 `tools` 一起校验。

### 4.5 report 要求

输出 `<dataset>.report.json`，至少包含：

- 输入/输出 rows、sessions；
- `state_counts`：running / pause / resume / finalize；
- pause 点数、resume 点数；
- `delay_after_ms` 的 min/median/max；
- 因上下文被丢弃/裁剪的行数；
- 最大 header 长度；
- 被跳过 pause 的 session 数及原因。

### 4.6 数据校验清单

- [ ] 每个 pause 后紧跟同一 session 的 resume（按 session 内请求顺序，不要求全局 JSONL 相邻）；
- [ ] 不存在连续 pause；
- [ ] finalize 只在 session 最后一行；
- [ ] 最后一行没有被改成 resume；
- [ ] **非 null** 的 `delay_after_ms` 只出现在 pause/tool 行；为兼容老版本 `datasets` 的 Arrow schema 推断（以首行推断列），其他行保留 `"delay_after_ms": null`，worker 按 0 处理；
- [ ] `delay_after_ms > idle_offload_s*1000 + 5000`；
- [ ] 所有 prompt + max_out_len <= 32768；
- [ ] ON/OFF 使用同一份数据集；
- [ ] `messages/tools/session_id` 与输入完全一致。

## 5. AISBench 工具改动

### 5.1 新增控制字段 `delay_after_ms`

- 类型：整数，单位 ms；仅作为控制字段，不进入请求 body。
- 数据集列由 `CustomDataset` 自动转发到 `data`。
- 老版本 `datasets.Dataset.from_list` 用首行推断 Arrow schema，只出现在后续行的新列会被静默丢弃。因此**所有行都必须带 `delay_after_ms` 键**；非 pause/tool 行写 `null`，worker 按 0 处理。

### 5.2 在 session-aware worker 中执行 delay

文件：

```text
ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py
```

位置：`_session_aware_worker_loop` 的 `session_worker` 内，现有顺序为：

```python
await self.do_request(data, token_bucket, session)
...
if is_v2 and state.lower() in ("pause", "suspend", "finalize", "tool"):
    await self._report_agent_status(session, data, state)
if split:
    create_new_task(psid, q)
    ...
    break
```

要求改为：

```python
await self.do_request(data, token_bucket, session)
...
if is_v2 and state.lower() in ("pause", "suspend", "finalize", "tool"):
    await self._report_agent_status(session, data, state)

delay_ms = int(data.get("delay_after_ms") or 0)
if delay_ms > 0 and state.lower() in ("pause", "suspend", "tool"):
    await asyncio.sleep(delay_ms / 1000.0)

if split:
    create_new_task(psid, q)
    ...
    break
```

关键顺序：

```text
请求完成
  → POST pause
  → sleep(delay_after_ms)
  → 切新 task
  → 下一行 resume
  → POST running（触发 prefetch）
  → 请求
```

**不能**把 delay 放在 resume 请求内部：resume 的 `running` 会先取消 idle timer。

实现修正：`pause` / `suspend` 的 sleep 在**释放 session 并发槽之后**执行，避免 40s 等待占满 `session_concurrency` 导致有效并发塌陷、KV usage 下降和 `gate_skip`。`tool` 不切分 span，因此在 POST tool 后原地 sleep，下一行 resume 才能触发 prefetch。

`asyncio.sleep` 必须可被 `stop_event`/task cancel 打断，否则 shutdown 会卡住最长 delay 时长。

### 5.3 防止控制字段进入请求体

文件：

```text
ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py
```

在 `GenInferencer.do_request` 里，和 `timestamp / stage` 一起 pop：

```python
data.pop("delay_after_ms", None)
```

注意：`do_request` 内部会 `deepcopy(data)`，session worker 外层 `data` 仍能读到 `delay_after_ms`。

### 5.4 行为约束

- `delay_after_ms` 只对 pause/suspend/tool 生效；
- `resume` / `running` / `finalize` 不额外 sleep；
- `pass_agent_hint=False` 时如果数据没有 `delay_after_ms`，行为完全不变；
- 不改变现有 `agent_status` 单状态语义；本需求不要求新增 `agent_status_before/after`；
- `pause`/`suspend` 的 sleep 已移到释放 session 并发槽之后；`tool` 不切分 span，仍原地 sleep。`resume` 之前仍受 slot 限制，c 需按实际饱和点设置。

### 5.5 单元测试

必须覆盖：

- [ ] pause 行：`do_request` → POST pause → sleep(delay) → 新 task；顺序正确；
- [ ] resume 行：先 POST running，再发请求；
- [ ] tool 行：POST tool → sleep → 下一行 resume；
- [ ] `delay_after_ms` 不出现在请求 body；
- [ ] 非 pause 行不 sleep；
- [ ] `pass_agent_hint=False` 且无 delay 字段时行为不变；
- [ ] sleep 期间任务取消能正常退出。

## 6. 配置需求

### 6.1 数据集配置

新增：

```text
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_pause_offload.py
```

指向：

```text
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl
```

reader/infer/eval 配置保持现有 `swebench_trace_v2_finalize.py` 不变。

### 6.2 单实例 TP1 模型配置

新增/提供模板，关键字段：

```python
path="/home/RAG_data/models/Qwen3-8B"
model="kvh"
stream=True
use_chat_api=True
session_aware=True
session_concurrency=8      # 压测时按 ramp 调整
agent_hint_transport="header"
pass_agent_hint=True       # KVC on
agent_status_report=True
json_extra_fields=["messages", "tools"]
use_timestamp=False
batch_size=8
host_ip="127.0.0.1"
host_port=19325
max_out_len=4096
generation_kwargs=dict(temperature=0, ignore_eos=True)
```

KVC-off 对照臂使用同一模型配置，只关闭 Motor 的 KVC lifecycle。

### 6.3 Motor 配置

单实例 TP1：

- 1 个 engine，TP1，1 张卡；
- `MMC_WORLD_SIZE=1`；
- `NUM_ENGINES=1 NUM_WORKERS=1`；
- 建议 arm：`a5`（保留亲和，单实例下不影响路由）；
- KVC-on：

```text
agent_status_config.enabled=true
agent_status_config.central_lifecycle=true
kvc_lifecycle_config.enabled=true
kvc_lifecycle_config.idle_offload_s=30
kvc_lifecycle_config.idle_evict_s=0
kvc_lifecycle_config.offload_kv_usage_high_water=0.7
kvc_lifecycle_config.prefetch_enabled=true
```

- KVC-off 对照：

```text
agent_status_config.enabled=true
agent_status_config.central_lifecycle=false   # 或保留 true 也可
kvc_lifecycle_config.enabled=false
```

要求 pause/resume 的 `/agent-status` POST 仍被接受，只是不执行 offload/prefetch/evict。

## 7. 压测与 A/B 方案

### 7.1 先确认压满

单实例 TP1，1 张卡，32768 上下文。建议 ramp：

```text
session_concurrency = 8 → 12 → 16 → 24 → 32
```

每一步用 1 秒采样：
- `vllm:kv_cache_usage_perc`
- `vllm:num_requests_waiting`
- `vllm:num_preemptions_total`
- `vllm:num_requests_running`

选择满足以下之一的最小 c：

- `Kv cache usage` 持续 ≥70%；或
- `Waiting > 0` / 出现 preemption。

若 c=32 仍打不满，再提高输出长度到 1024/2048，或复制 session（复制时必须在 messages 最前面加唯一 system 前缀，避免 prefix cache 共享导致 KV 虚低）。

### 7.2 KVC A/B

同一份 pause/offload 数据、同一 c、同一 warmup、同一引擎：

| 臂 | Motor KVC | 预期 |
|---|---|---|
| KVC-on | enabled | pause → offload；resume → prefetch；finalize → evict |
| KVC-off | disabled | pause/resume 只更新状态，无 engine 动作 |

每臂至少 1 次，最好 2 次交替；重启 engine/Motor 清空状态。

### 7.3 必查日志与指标

AISBench：

- `[agent_hint][agent-status] ... state=pause`
- `[agent_hint][agent-status] ... state=running`（resume）
- `[agent_hint][agent-status] ... state=finalize`

Motor：

- `kvc offload ...` / `kvc prefetch ...` / `kvc evict ...`
- `motor:kvc_offload_total`
- `motor:kvc_prefetch_total`
- `motor:kvc_evict_total`
- `motor:kvc_edit_success_total` / `edit_fail_total`
- `motor:kvc_gate_skip_total`

Engine：

- `Executing edit type=offload/prefetch/evict`
- `actual_op_block_num`
- `kv_cache_usage_perc`

### 7.4 pause 到 offload 的时序（为什么必须有 inter-request delay）

`POST /agent-status {state:pause}` **不是立刻 offload**。

pause 行的实际顺序：

```text
AISBench 发请求
  → 收到响应
  → POST /agent-status {state:pause}
  → Motor: state=pause，取消旧 timer
  → Motor: _schedule_idle_locked() 启动 asyncio.sleep(idle_offload_s)
  → （AISBench 默认立即处理下一行）
```

如果下一行是 `resume`，AISBench 会在发请求前先 POST `/agent-status {state:running}`；Motor 收到 running 后会：

```text
state=idle_generation+1
  → 取消该 session 的所有 idle timer
  → 后续不会再有 offload
```

所以没有 delay 时：

```text
pause → resume(running) → timer 被取消 → offload 永不触发
```

有 delay 时：

```text
pause行：请求 → POST pause → sleep(delay_after_ms)
                                      ↓
                              idle timer 到期
                                      ↓
                          Motor 发 engine offload
                                      ↓
resume行：POST running → 触发 prefetch → 发请求
```

关键约束：

- `delay_after_ms` 必须放在 **pause POST 之后、resume 的 running POST 之前**；
- 如果把 delay 放在 resume 请求内部，`running` 已经先发出去，timer 已被取消，offload 仍然不会发生；
- `delay_after_ms` 必须 **大于** `idle_offload_s*1000`，并留出 offload 执行时间（真实档：idle=30s，delay=40s）；
- `idle_offload_s=0` 表示关闭 offload，不是“立刻 offload”；
- 即使 delay 足够，`offload_kv_usage_high_water=0.7` 时如果 KV usage 未达到阈值，`_execute` 会 `gate_skip`，也不会真正 offload；
- `tool` 状态同样走 idle timer，因此也需要 delay；`finalize` 则是立即 enqueue evict，和 pause 的 timer 语义不同。

### 7.5 prefetch 是否能起到预热效果（重要限制）

当前 prefetch 是**异步提交**，不是同步预热，不能假定 resume 请求一定命中已预取的 KV。

时序：

```text
resume 行
  → POST /agent-status {state:running}
  → Motor 发现 session.offloaded，发 engine prefetch 管理请求
  → engine _execute_prefetch() 只把 PrefetchRequest 加入 prefetch_waiting_queue
  → manage request 返回 op_status=true（表示已入队/已提交）
  → AISBench 立即发 resume 请求
  → engine 在后续 on_step() 才处理 prefetch_waiting_queue
```

关键代码路径：

- `vllm_ascend/core/agent_hint/session_aware_manager.py::_execute_prefetch`：只 notify SPM，返回 block 数；
- `vllm_ascend/core/agent_hint/session_aware_pooling_manager.py::on_context_management_prefetch`：只 append 到 `prefetch_waiting_queue`；
- `process_prefetch_req` 在 engine `on_step()` 才分配 slot、提交 KV connector，并放到 `prefetch_running_queue`；
- `prefetch_total` / `op_status=true` 只代表提交成功，不代表数据已经回到 HBM。

因此在当前 pause→delay→resume 序列里：

- delay 是在 **pause 后、resume 前**，只保证 offload 有机会发生；
- prefetch 是在 **resume 的 running POST 时** 才触发，紧接着就发请求，几乎没有 prefetch lead time；
- resume 请求可能在 prefetch 真正完成前到达，实际仍要自己等 KV 加载，预热效果有限且不稳定。

若要验证/获得预热效果，需要额外做一项：

- 增加 `prefetch_lead_ms`：POST running 触发 prefetch 后，再 sleep `prefetch_lead_ms` 才发 resume 请求；或
- 把 prefetch 触发提前到 pause/offload 之后（预测型预取）；或
- 让 engine 的 prefetch 管理请求同步等待数据真正 resident（改动较大，当前实现不保证）。

测试时必须区分：

- `prefetch_total`：只说明发起了多少次 prefetch；
- engine 日志 `prefetch req ... local hit tokens` / `free prefetch request`：看实际处理进度；
- resume 请求的 TTFT 和 `prefix_cache_hits_total` 增量：看是否真的预热成功；
- 若 `prefetch_total>0` 但 TTFT 无改善，不能直接归因于 KVC 无效，可能是 prefetch 没有 lead time。

### 7.6 验收口径

- `offload_total > 0`，且与 pause 点数大致匹配（受 high_water gate 影响）；
- `prefetch_total > 0`，且 ≤ offload_total；
- `evict_total` 与 finalize 数匹配；
- `edit_fail=0`、`send_fail=0`；
- 饱和条件下，KVC-on 相对 KVC-off：
  - P99 不恶化，且吞吐不下降；或
  - 在相同 KV usage 下支持更高并发；或
  - 在相同并发下 KV usage 更低、Waiting 更少。
- 若以上都没有改善，报告“该负载下 KVC 无收益”，不要强行归因。

## 8. 风险与约束

1. **delay 与 session 槽**：`pause`/`suspend` 已改为释放 session 槽后 sleep，不会因等待占满 c；但 resume 仍需要槽，压测时 c 仍按饱和点 ramp。
2. **KVC gate**：`offload_kv_usage_high_water=0.7` 时，KV 未到 70% 会 `gate_skip`，offload 不会发生；必须先压满。
3. **真实档时长**：idle=30s、delay=40s 会显著拉长 run；按 pause 点数预估总时长（例如 80 个 pause ≈ 53 分钟额外等待）。
4. **finalize 与 offload 并存**：本需求保留 finalize evict；报告需区分 offload/prefetch 与 finalize evict 的计数。
5. **TP1 容量更小**：单卡更容易压满，但也更容易 OOM/排队；ramp 时观察 Waiting/preemption，不要一上来 c=32。
6. **不要用 legacy body manage_request 代替**：本需求验证的是 `/agent-status` pause/resume 生命周期，不是 legacy body 的 offload/prefetch 命令。
7. **不要复制完全相同 prompt**：若为了增加 session 复制数据，必须加唯一前缀，否则 prefix cache/KV 共享会把压力测低。

## 9. 交付物

- [x] `build_agent_dataset_v2_pause.py`（或 builder 的 `pause-offload` 模式）；
- [x] `swebench_full50_v2_pause_offload.jsonl` + `*.report.json`；
- [x] `delay_after_ms` 的 AISBench 实现与单测；
- [x] `swebench_trace_v2_pause_offload.py` 数据集配置；
- [x] 单实例 TP1 模型配置模板；
- [x] 一份压测结果模板：c ramp、KVC on/off、日志与指标；
- [x] 本文档更新到 `docs/source_zh_cn/index.rst`（可选）。

## 10. 非目标

- 不在本需求验证多引擎会话亲和收益；
- 不修改 v2 Header schema（不新增 `agent_status_before/after`）；
- 不实现 256k 上下文；
- 不替换现有 `running/finalize` 数据集；
- 不改变 hints off 的默认行为。

## 11. 实现修正与基线（已落地）

相对初版需求，落地实现做了以下修正：

1. **pause 点索引**：按 1-based 的第 N、2N… 个请求选点；候选点下一行必须存在且不是最后一行。因此 session 至少需要 `N+2` 个请求（默认 5）。
2. **不伪造 finalize**：输入中未以 finalize 结尾的 session 保留 running，并在 report 记为 `unfinished_sessions`。基线数据有 7 个此类 session，finalize 仍为 51。
3. **上下文校验前移**：先按 tokenizer（messages + tools + chat template）校验并丢弃/裁剪，再插入 pause，避免丢弃行造成孤立 pause/resume。基线有 7 行 prompt 本身超过 32768，被丢弃；输出 324 行。
4. **不添加 `cache_control`**；hint 只含 session 与 agent_status。
5. **pause/suspend 的 sleep 在释放 session 槽之后执行**，并用 `stop_event` 可中断；`tool` 原地 sleep。
6. **delay 控制字段**：`icl_gen_inferencer.py` 的 `do_request` pop `delay_after_ms`，session worker 从外层 data 读取；不会进入请求 body。所有行都保留 `delay_after_ms` 键（非 pause/tool 行为 `null`），否则老版本 `datasets.Dataset.from_list` 会按首行推断 schema 并静默丢列。

基线数据（`--pause-every 3 --delay-ms 40000 --jitter-ms 0`）：

| 项 | 值 |
|---|---|
| 输入 / 输出 rows | 331 / 324 |
| sessions | 58 |
| pause / resume 点 | 65 / 65 |
| 有 pause 的 session | 20 |
| 未插入 pause 的 session | 38 |
| finalize | 51 |
| unfinished sessions | 7 |
| max header length | 308 |
| prompt tokens min / median / max | 13971 / 24563 / 31379 |

产出文件：

```text
ais_bench/datasets/agent/build_agent_dataset_v2_pause.py
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl.report.json
ais_bench/benchmark/configs/datasets/custom/swebench_trace_v2_pause_offload.py
ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session_hint_v2_pause_offload.py
docs/source_zh_cn/advanced_tutorials/session_aware_agent_hint_v2_kvc_offload_results_template.md
```

单测：

```text
tests/UT/datasets/agent/test_build_agent_dataset_v2_pause.py
tests/UT/openicl/icl_inferencer/test_agent_hint_scheduling.py
tests/UT/openicl/icl_inferencer/test_icl_gen_inferencer.py
```
