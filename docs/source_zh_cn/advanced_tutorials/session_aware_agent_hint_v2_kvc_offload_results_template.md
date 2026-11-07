# Agent Hint V2 KVC Pause/Offload 压测结果模板

配套需求：`session_aware_agent_hint_v2_kvc_offload_requirement.md`。
本文档给出修正后的数据生成方式、单实例 TP1 配置、c ramp / KVC A/B 记录表和验收口径。

## 1. 数据集生成

输入：

```text
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl
```

输出：

```text
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl
/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl.report.json
```

生成命令：

```shell
python ais_bench/datasets/agent/build_agent_dataset_v2_pause.py \
  --input /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl \
  --output /home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl \
  --pause-every 3 \
  --min-session-requests 3 \
  --idle-offload-s 30 \
  --delay-ms 40000 \
  --jitter-ms 0 \
  --tokenizer /home/RAG_data/models/Qwen3-8B \
  --max-context 32768
```

当前基线（`pause-every=3`，无 jitter）：

| 项 | 值 |
|---|---|
| 输入 / 输出 rows | 331 / 324 |
| 因 prompt 超过 32768 丢弃 | 7 |
| sessions | 58 |
| pause / resume 点 | 65 / 65 |
| 有 pause 的 session | 20 |
| 未插入 pause 的 session | 38（`too_few_requests` 26、`no_following_row` 8、`next_is_finalize` 3、`next_is_last_row` 1） |
| 未 finalize 的 session | 7（保留原样，不合成 finalize） |
| max header length | 308 |
| prompt tokens min / median / max | 13971 / 24563 / 31379 |
| delay_after_ms | pause 行全部 40000（阈值 35000）；其他行为 `null`（兼容 Arrow schema 推断） |

> 修正点：pause 放在 session 的第 N 个请求（1-based）上，下一请求改为 resume；最后一行永远不会被改成 resume。因此一个 session 至少需要 N+2 个请求才能容纳 pause → resume → finalize。不要强行把未 finalize 的 session 改成 finalize。

## 2. 数据集配置

```shell
ais_bench --models vllm_api_general_stream_session_hint_v2_pause_offload \
          --datasets swebench_trace_v2_pause_offload \
          --mode perf
```

模型配置关键字段：

```text
pass_agent_hint=True
agent_status_report=True
agent_hint_transport="header"
session_aware=True
session_concurrency=8        # 按 ramp 调整：编辑配置里的 _C，或复制配置
use_timestamp=False
json_extra_fields=["messages", "tools"]
```

ON/OFF 两臂使用**同一份数据集、同一模型配置**，只切换 Motor 的 KVC lifecycle。
每臂之间必须重启 Motor/engine，因为 Motor 中 `finalize` 是终态。

## 3. Motor KVC 配置

KVC-on：

```text
agent_status_config.enabled=true
agent_status_config.central_lifecycle=true
kvc_lifecycle_config.enabled=true
kvc_lifecycle_config.idle_offload_s=30
kvc_lifecycle_config.idle_evict_s=0
kvc_lifecycle_config.offload_kv_usage_high_water=0.7
kvc_lifecycle_config.prefetch_enabled=true
```

KVC-off：

```text
agent_status_config.enabled=true
agent_status_config.central_lifecycle=false   # 保持 true 也可
kvc_lifecycle_config.enabled=false
```

要求 pause/resume 的 `/agent-status` POST 仍返回 200（可能 `applied=false`），只是不执行 offload/prefetch/evict。

## 4. c ramp 记录

单实例 TP1，建议 `session_concurrency = 8 → 12 → 16 → 24 → 32`。每步至少运行 2 分钟，1 秒采样。

| c | KV usage p50 | KV usage max | Waiting max | preemptions | running max | 吞吐 (req/s) | TTFT p50 | TTFT p99 | 是否压满 |
|---|---|---|---|---|---|---|---|---|---|
| 8 | | | | | | | | | |
| 12 | | | | | | | | | |
| 16 | | | | | | | | | |
| 24 | | | | | | | | | |
| 32 | | | | | | | | | |

压满判据（满足其一）：

- KV usage 持续 ≥ 70%；
- `vllm:num_requests_waiting > 0`；
- 出现 `vllm:num_preemptions_total` 增长。

## 5. KVC A/B 记录

同一数据集、同一 c、同一 warmup、同一引擎。每臂至少 1 次，最好 2 次交替。

| 臂 | Motor KVC | 运行编号 | 总请求 | 成功率 | 吞吐 | TTFT p50 | TTFT p99 | E2E p50 | E2E p99 | KV usage p50 | Waiting max | preemptions |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| KVC-on | enabled | | | | | | | | | | | |
| KVC-off | disabled | | | | | | | | | | | |
| KVC-on (repeat) | enabled | | | | | | | | | | | |
| KVC-off (repeat) | disabled | | | | | | | | | | | |

Motor 指标：

| 指标 | KVC-on | KVC-off | 说明 |
|---|---|---|---|
| `motor:kvc_offload_total` | | | 应 > 0，且与 pause 点大致匹配（受 high_water gate 影响） |
| `motor:kvc_prefetch_total` | | | 应 > 0，且 ≤ offload_total |
| `motor:kvc_evict_total` | | | 与 finalize 数匹配（51） |
| `motor:kvc_edit_success_total` | | | |
| `motor:kvc_edit_fail_total` | | | 应为 0 |
| `motor:kvc_gate_skip_total` | | | 记录 high_water gate 跳过的次数 |

## 6. 日志检查

AISBench：

- [ ] `[agent_hint][agent-status] ... state=pause`
- [ ] `[agent_hint][agent-status] ... state=running`（resume）
- [ ] `[agent_hint][agent-status] ... state=finalize`
- [ ] `[agent_hint][send-header] ... state=pause/resume` 存在

Motor：

- [ ] `kvc offload ...` / `kvc prefetch ...` / `kvc evict ...`
- [ ] `agent_status event ... applied=True`
- [ ] `Executing edit type=offload/prefetch/evict`

Engine：

- [ ] `kv_cache_usage_perc` 有对应变化
- [ ] `actual_op_block_num` 有记录

## 7. 验收

- [ ] 数据 checklist 全部通过（见 report 的 `checks.all_passed`）
- [ ] `offload_total > 0`，且与 pause 点大致匹配
- [ ] `prefetch_total > 0` 且 ≤ `offload_total`
- [ ] `evict_total` 与 finalize 数匹配
- [ ] `edit_fail=0`、`send_fail=0`
- [ ] 饱和条件下 KVC-on 相对 KVC-off 满足任一：
  - P99 不恶化且吞吐不下降；或
  - 相同 KV usage 下支持更高并发；或
  - 相同并发下 KV usage 更低、Waiting 更少。
- [ ] 若无改善，报告“该负载下 KVC 无收益”，不强行归因。

## 8. 已知风险

1. `idle_offload_s=30` + `delay_after_ms=40000` 只给 offload 留约 10 秒窗口；若 offload 较慢，resume 可能赶在 offload 完成前。可提高到 60–90 秒，或在 resume 前确认 Motor 指标。
2. `offload_kv_usage_high_water=0.7` 会在 KV 未到 70% 时 gate_skip；offload 本身又会降低 KV usage，可能自限。建议额外跑一个 `high_water=0` 的 offload-only arm 验证链路。
3. pause 的 40 秒等待已改为**释放 session 槽后再 sleep**，不会占满 `session_concurrency`；但 resume 仍受 slot 限制，c 仍需按实际饱和点设置。
4. 未 finalize 的 7 个 session 不会触发 evict；报告需区分 offload/prefetch 与 finalize evict。
