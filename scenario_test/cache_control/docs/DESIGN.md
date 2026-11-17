# 设计：短 pause 场景下 cache_control（agent hint）的收益机制

## 1. 要治的病

Agent 干活是「思考 → 调工具 → 等结果」：等工具的 60 s 里会话不发请求，但它的 KV 前缀还占在引擎里。
若 pause 期间别的会话持续分配 KV，pause 会话的块会被回收；resume 回来前缀全丢，
8.4 k token 从头 prefill —— 这就是 TTFT 慢的来源。

hint 的作用：客户端在请求头里声明「我这是 agent 会话，pause 完还会回来」，
引擎据此把该请求前缀的 KV 块登记成「受保护」，pause 期间不回收给新分配。

## 2. hint 链路（header 版）

```
replay_driver.py
  ① 把数据行的 agent_hint 序列化成 b64url，放 X-Agent-Hint 头（--hint-mode header）
        ↓
Motor coordinator（router/dispatch.py）
  ② apply_agent_hint_header                解码
  ③ apply_spec_cache_control_to_engine_body
        ttl_s -> ttl（clamp [60,3600]）
        range{end, unit:"message"} -> msg_offset: end
        unit != "message" 或 range.start ∉ {None,0} ⇒ 整段 range 静默丢弃
  ④ parse_agent_hint                       组装 engine body 的 agent_hint
        ↓
引擎（vLLM :18220，VLLM_ASCEND_ENABLE_AGENT_HINT=1）
  ⑤ register_agent_hint / register_session_block_hash
  ⑥ on_blocks_allocated / on_block_cache_hit -> 受保护块 hash 进 TTL 管理器
  ⑦ ttl_s 内不被 pop；到期或被覆盖才释放
```

状态旁路：`agent_status`（running / pause / resume / finalize）走
`POST :19476/internal/agent-status`（404 时回落 `:19477`），**200 才算被接受**。
它只做状态同步，不承载 `cache_control`。

## 3. hint 语义（写错就没有保护）

```json
{"type": "ephemeral", "ttl_s": 120, "range": {"end": 4, "unit": "message"}}
```

- `ttl_s` 是**保护窗口**，必须 > pause（本场景 pause 60 s ⇒ ttl 120 s）。
- `range.end` 是**消息下标**：本数据集 `messages` 恒 4 段 ⇒ `end=4` 覆盖整段前缀。
- `unit` **必须**是 `message`；`range.start` 只能缺省或 0。

## 4. 数据集为什么长这样

| 性质 | 值 | 作用 |
|---|---|---|
| resume 占比 | 224/256 = 87.5% | 让 p50 落在 resume 档，命中收益直接变成 p50 收益 |
| pause | 60 s | 远大于块周转（12–20 s）⇒ 关 hint 必丢 |
| prompt | 8,448 token = 66 块 | 固定长度，保护需求 = 会话数 × 66，可纯算术算容量 |
| 前缀结构 | 前 3 段恒定（system + 8.6 k 长文档 + ack），第 4 段每调必变 | 典型 agent 上下文：长前缀复用 + 小增量 |
| 会话间长文档不同 | — | 每个会话独占 66 块工作集，容量账干净（无跨会话共享） |
| max_out_len 64 | decode ≈ 0.8 s | 8 s 槽够用，周期由 pause 主导（256 会把槽撑爆） |

## 5. 引擎状态污染（实验纪律，2026-11-17 实测确认）

**现象**：同一引擎进程先跑过任何 hints-on 流量后，再跑 off，off 的 resume 全部退化为全量重算
（p50 ~1020–1028 ms）；而干净引擎（刚重启、没跑过 hint）上 off 的 p50 只有 ~186–200 ms。
间隔 24 s / 49 min / 2.4 h 都一样，**不自愈**。`reset_prefix_cache` 返回 200 也救不回来。

**机制**：hints-on 会在引擎进程内留下三类状态，`reset_prefix_cache` 都清不掉
（代码里没有 reset 钩子）：

1. 会话注册表（session → block-hash 序列）累积，不随前缀缓存清空而释放；
2. 受保护块（C 区）继续占用可分配窗口，压缩了普通会话的可用空闲块；
3. `num_free_blocks` 把受保护块也算成「空闲」，调度器读到的余量虚高。

**后果**：off 会话的前缀在 60 s pause 里被这些「虚高空闲 + C 区占地」挤掉，
resume 全量重算。因此**未重启引擎的 off 不是干净基线**——它是被 hint 流量人为劣化的基线。

**纪律**：
- 切臂（尤其 decl → off）必须 `docker restart` 引擎进程（`02_start_engine.sh --restart`）；
- motor 也要容器级重启（清 SHM/旧 coordinator 残留）；
- 每轮把「该引擎实例此前是否有 hint 流量」写进证据（包内 `_run_meta.json`），
  V9 判据据此作废被污染的 off 轮。

**两种可报的结论（不可混说）**：

| 形态 | 可报结论 |
|---|---|
| 干净引擎 | 池子装得下时 hint 是**纯开销 +15–20 ms**；S1 保护过度会反向劣化 |
| 混部引擎 | hint 会话相对 off 会话有 **~80% 的会话级优势**（生产混合流量形态） |

## 6. 容量：硬边界（与污染无关）

> 注意：早期用「churn(块/s) vs pause」推导的「off 必丢」结论，是在**被污染的引擎**上测的。
> 干净引擎 + 会话数不大时，LRU 复用距离 < pause，off 本就能命中；
> 真正让 off 必丢的是**容量不足**（需求 > 池子），例如 3.2 上 S32（3.38 倍）、169 上 S32（1.04 倍边缘）。

```
保护需求 = 会话数 × 66 块
准入线   = 池子块数 ≥ 1.05 × 保护需求
```

| 需求/池子 | 实测现象（32GB 624 块 / 64GB 2031 块） |
|---|---|
| ≤ 0.85 | 命中 74–89%，p50 降 58–80%（S4/S8@3.2、S32@169） |
| 1.04 | 勉强装下：命中 72–74%，p50 −57…−69%（S32@169） |
| 1.32–1.76 | 收益归零，落在 off 区间 |
| ≥ 2.6 | **开比不开更慢**（800 块：p50 1,151.9、mean 1,322.0，比四轮 off 都差） |

**KV 占用率不是压力指标**：off 臂峰值占用只有 0.70–0.74（本场景 S8 甚至只有 0.11），
块照样被 pop 干净。要看的是 **churn（块/s）× pause** 和空闲块周转时间。

## 7. 引擎侧退化机制（为什么会「开比不开更慢」）

引擎已有三层：分区（普通/缓冲/受保护）、会话级 LRU 淘汰、TTL 到期提升。
缺的那一维是**块水位准入**：发保护许可时只看会话自己的块序列，不看池子已留出多少；
而且 `append()` 把受保护块也计入「空闲」，调度器读到的余量是虚高的。
⇒ 超容时保护块被后续请求占掉，占的过程还要多做淘汰判断（纯开销），最后双方都没命中。
本场景的容量准入规则就是用来绕开这一维缺口的。

## 8. 可观测性口径

| 量 | 来源 | 说明 |
|---|---|---|
| Δhits | `vllm:prefix_cache_hits_total` 前后差 | 含 warmup（16,640） |
| Δqueries | `vllm:prefix_cache_queries_total` | 分母不稳，**只能同机比** |
| Δprefill | `vllm:request_prefill_kv_computed_tokens_sum` | **最硬的证据**；off 臂多轮逐位相同 |
| TTFT | `requests.jsonl` 的 `ttft_ms` | p50 = nearest-rank |
| hit/pt | Δhits / Σ prompt_tokens | **唯一可跨机比**的命中率口径 |
| 复用块 | (Δhits − 16,640) / 128 | off 臂恒 0 |
| 恒等式 | Δhits + Δprefill == Δqueries | 逐轮精确成立，不闭合则该轮作废 |

请求级 cached 不可观测（usage 没有 `prompt_tokens_details.cached_tokens`），命中量一律用块数表达。
