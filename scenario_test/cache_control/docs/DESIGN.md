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

## 6. 收益的开关：淘汰压力（统一法则）

> **统一判据**：无保护块在 pause 期间被淘汰 ⟺ `churn × pause > 空闲块`。
> 其中「churn」是所有**未命中分配**的速率（外部流量 + 本会话自身 miss），
> 「空闲块」= 池子 − 保护需求。

这条法则把三类实验统一起来，它们只是「churn 从哪来」不同：

| 形态 | churn 来源 | 空闲块 | 结果 |
|---|---|---|---|
| clean（S8，无外部流量） | ≈0（命中后零新分配，自我衰减） | ~57–97 | 块不被淘汰 ⇒ **off 天然命中 86% ⇒ hint 纯开销 +19 ms** |
| churn（8 agent + 外部冷流量） | 11.3 / 22.7 / 32.4 块/s | ~57 | 空闲块十几秒被 pop 光 ⇒ **off 全崩；decl 保住 96% 的块** |
| 169 S32（容量边界） | 32 会话自身 miss（自生） | 负数（超容） | off 因容量丢前缀；命中 72–74% |

**容量准入**（必要条件，不是充分条件）：

```
保护需求 = 会话数 × 66 块          （实测含输出/追加轮次时 71 hash/会话，见 §9）
准入线   = 池子块数 ≥ 1.05 × 保护需求
```

| 需求/池子 | 实测现象（32GB 624 块 / 64GB 2031 块） |
|---|---|
| ≤ 0.85 | 装得下；但**是否有收益取决于外部压力**（无压力 ⇒ hint 纯开销） |
| 1.04 | 勉强装下：命中 72–74%，p50 −57…−69%（S32@169） |
| 1.32–1.76 | 收益归零，落在 off 区间 |
| ≥ 2.6 | **开比不开更慢**（800 块：p50 1,151.9、mean 1,322.0，比四轮 off 都差） |

**KV 占用率不是压力指标**：off 臂峰值占用只有 0.70–0.74（S8 甚至 0.11），块照样被 pop 干净。
要看的是 **churn（块/s）× pause** 与空闲块周转时间。

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

## 9. 形态 C：受控外部 churn（2026-11-17 引入）

### 9.1 为什么需要它

形态 B（污染引擎）能给出 ~80% 的会话级差距，但它依赖「引擎状态被 hint 流量破坏」，
本身不可验收（V9 必然 FAIL）、也不可移植。形态 C 用**受控外部冷流量**在干净引擎上
复现同一机制：

- churn 行 = 一次性冷流量：长文档按偏移切片（步长 400 字符 > 一个 128-token 块长），
  前缀永不重复 ⇒ 每次全量 miss ⇒ 持续从 free queue 取块；
- churn 行**没有 `cache_control`** ⇒ 即使 decl 臂给它们发了 header，也没有保护语义
  （`requests.jsonl.cc_raw` 为空），不占保护额度；
- 两臂跑同一份 churn 数据集，唯一差别仍是「agent 行受不受保护」⇒ 干净对照。

### 9.2 块账（实测标定，不是假设）

| 量 | 实测值 | 来源/推算 |
|---|---|---|
| agent prompt | 8,448 token = 66 块 | 数据集固定 |
| 保护 hash 长度 | **71/会话**（55→71 随调用增长） | 引擎日志 `protected hash length` |
| 8 会话保护占用 | **568 块**（624 池子的 91%） | 8 × 71 |
| 可牺牲区 A+B | **≈57 块** | 624 − 568（±8，随 banner 抖动） |
| churn 每请求 | **≈2,905 token ≈22.7 块** | 由 Δqueries 反算（勿用假设值） |
| churn 稳态占用 | ~23–58 块 | 在飞 + free queue 待 pop |
| L3（0.7 s/请求）余量 | ≈1.2 倍 | 58 / 57 ⇒ 排队成本显现、保护未破 |

> 早期文档按「4,096 token/32 块」估算 churn 压力，实测高估约 45%；压力档位
> 应以 `06_analyze.py` 的 `press` 列为准（L1/L2/L3 ≈ 11.3 / 22.7 / 32.4 块/s）。

### 9.3 口径：只报 agent 组

churn 形态的统计单位是 **agent 会话**，不是「所有请求」：

| 口径 | off | decl | 降幅 |
|---|--:|--:|--:|
| agent 组 p50 | 1,186 | 212 | −82% |
| 全体 p50（含 churn） | 356 | 339 | −5% |

churn 行占 ~90% 请求、且两臂必须行为一致（无保护声明），混入 p50 会把效应稀释成噪声。
`06_analyze.py` 同时输出 `agP50` 与 `allP50`，结论只引用前者。

### 9.4 判据：V10 与 V11

- **V10（压力有效性，off 轮）**：`agent hit/pt ≤ 5%`。若 off 也能命中，说明 churn 太弱、
  这一轮没有压力，收益不可归因于 hint。
- **V11（收益兑现，decl 轮）**：`decl agent resume p50 ≤ 0.5 × 配对 off 的 resume p50`。
  防止「跑了但没效果」的轮次被当成成功。

### 9.5 实测结果（L2 档，干净引擎，每轮重启）

| 臂 | agent p50 | agent mean | Δhits | 复用块 | 恒等式 |
|---|--:|--:|--:|--:|---|
| off ×2 | 1,174.0 / 1,173.9 | 1,173–1,195 | 5,120 | ~0 | 精确闭合 2,303,742 |
| decl ×2 | **218.4 / 232.7** | 406–414 | 454,912 | 3,554（96.2%） | 精确 / +0.13% |

**p50 降幅 −80.8%**。交税的是冷启动：decl 冷请求 1,277–1,280 ms，比 off 冷请求（1,189）
慢约 +90 ms（注册 + 排队），但 87.5% 的 resume 快 4–5 倍。

### 9.6 已知边界

- 承压拐点（保护被突破）**未触及**：32.4 块/s 下 Δhits 与低档完全一致（3,554），
  只是排队让 decl 延迟从 226 升到 284 ms；
- churn 是合成背景流量，真实背景流量的长尾/突发形态未覆盖；
- 恒等式在 churn 场景有偶发 ±0.14% 采样竞态（在飞 churn 请求跨窗口边界）；判据容差取 0.25%。
