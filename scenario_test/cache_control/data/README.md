# 数据集：sp7 短 pause（60 s）

## 数据契约

| 项 | 值 |
|---|---|
| 行数 | `N 会话 × 16` = `8N infer + 8N event` |
| infer 行 | 每会话 8 次：1 次 `state=running`（冷启）+ 7 次 `state=resume`（占 87.5%） |
| event 行 | 7 次 `pause` + 1 次 `finalize`（只发状态 POST，不推理） |
| 时间结构 | 会话内周期 68 s = pause 60 s + 槽 8 s；会话间错峰 8 s |
| prompt | 恒 4 段 messages；前 3 段（system + 8,609 字符长文档 + "ack"）会话内恒定，第 4 段每调必变；合计 **8,448 token = 66 块** |
| 会话间前缀 | 长文档互不相同 ⇒ 每会话独占 66 块工作集（容量账干净） |
| `cache_control` | `{"type":"ephemeral","ttl_s":120,"range":{"end":4,"unit":"message"}}`（header 版） |
| `max_out_len` | infer 64 / finalize 256；finalize 不参与 prefill |
| 设计 span | `(N-1)×8 + 7×68 + 8` 秒（S32=732 s、S8=540 s、S4=508 s、S1=484 s） |

## 为什么冻结源而不现场生成源

`source/contend_lru60_S32.jsonl` 的上游生成器 `gen_contend.py` 需要**活引擎做 token 校准**、
且依赖一个已不在仓库里的真实 capture 模板（`acp_pausereplay_smoke_e2.jsonl`），无法离线重建。
因此把这份源**冻结**进来（2.5 MB），生成器只做纯离线变换。

- 源文件：`source/contend_lru60_S32.jsonl`
  - md5 `267b823d021f22a68f57505c382e04b1`
  - 2,547,328 B

## 生成方式

```bash
make data          # 等价于 python3 data/gen_sp7.py --sessions 1,4,8,32
```

输出到 `data/generated/`（**不入 git**）。同一输入是字节级确定性的，sha256 如下：

| 文件 | 行数 | infer | sha256 |
|---|--:|--:|---|
| `contend_sp7_p60_S1.jsonl` | 16 | 8 | `8efda7a7ce9a6f883dd6d5bab9af17f3845bd81e009ab414a9b22260e5ae1346` |
| `contend_sp7_p60_S4.jsonl` | 64 | 32 | `039eabcb4859e6317c4329fb5dc5e3ba51d36d613145b2a82976184942f8c292` |
| `contend_sp7_p60_S8.jsonl` | 128 | 64 | `d509d4332ccc2fb9ead09b0d2b12b03ca178bccaf50422c6a14cfc0507b9d549` |
| `contend_sp7_p60_S32.jsonl` | 512 | 256 | `994336a53213f74fbb1450695eeb45eb11e18b21b0b89fedccbd01be0cee7d9f` |

`S1/S4/S8` 是 `S32` 前 N 个会话的**逐字节相同**子集（已验证）——这就是容量阶梯实验的干净设计。

`05_run_matrix.sh` 在跑之前会用 `config/scenario.json` 里的 sha256 逐个校验，不匹配拒跑。

## 规模 → 容量对照（块，128 token/块）

| 数据集 | 会话 | 保护需求（会话×66） | 624 块池（32GB 卡） | 2,031 块池（64GB 卡） |
|---|--:|--:|--:|---|
| S1 | 1 | 66 | 0.11 | 0.03 |
| S4 | 4 | 264 | 0.42 ✅ 装得下 | 0.13 ✅ |
| S8 | 8 | 528 | **0.85 ✅ 装得下**（本机正收益点） | 0.26 ✅ |
| S32 | 32 | 2,112 | 3.38 ❌ 收益归零 | **1.04 ✅ 勉强装下**（169 正收益点） |

> 准入规则：`池子块数 ≥ 1.05 × 会话数 × 66`。低于这条线，hint 从「加速」变成「无效」，
> 再低变成「拖慢」（800 块池实测 p50 1,151.9 ms、mean 1,322.0 ms，比四轮 off 都差）。

---

## 外部 churn 数据集（形态 C）

生成：`make data`（= `gen_sp7.py --sessions 1,4,8,32 --churn-intervals-ms 2000,1000,700`）

文件名：`contend_sp7churn_p60_S<N>_L<k>.jsonl`（k=1/2/3 对应 2.0/1.0/0.7 s 间隔）

| 项 | 值 |
|---|---|
| agent 行 | 与 `contend_sp7_p60_S<N>.jsonl` **逐字节相同**（含 event 行） |
| churn 行 | `kind=infer`、`session_id=churn-XXXX`、**无 `cache_control`**、`max_out_len=8` |
| churn 前缀 | 从源会话长文档按偏移切片（2,700 字符、步长 400 字符），按文档去重 |
| 唯一性 | 相邻切片首块不同（步长 > 一个 128-token 块长）⇒ 互不命中；池子用完复用也无害（旧块已被 pop） |
| churn 时刻 | `BASE + i×interval`，铺满 600 s（覆盖 S8 的 540 s span + 收尾） |
| 行数 | agent `16N + 8N` 中 infer 为 `8N`，churn 行 301 / 601 / 858 |

**语义**：churn 只制造**块淘汰压力**，不携带保护语义——即使 decl 臂给它们发了
`X-Agent-Hint`，header 里也没有 `cache_control`，`requests.jsonl.cc_raw` 为空。

**压力不是常数**：实测每请求 ≈2,905 token ≈22.7 块（由 Δqueries 反算），
所以三档压力为 ≈11.3 / 22.7 / 32.4 块/s。`06_analyze.py` 的 `press` 列给的是实测值，
不要用「4,096 token/32 块」这类假设。

沙箱校验（生成后应满足）：

```
L1: rows=429 agent_infer=64 churn=301 agent==S8=True churn_cc=0
L2: rows=729 agent_infer=64 churn=601 agent==S8=True churn_cc=0
L3: rows=986 agent_infer=64 churn=858 agent==S8=True churn_cc=0
```

---

## 过订比扫描数据集（bp / gp / gc，2026-11-18 新增）

生成：`make data-profit`（= `python3 data/gen_profit.py --ttl 60 --strip-cc`）

这一族**纯合成**（不依赖 capture、可离线逐字节复现），把两个真正的自变量做成旋钮：

| 旋钮 | 语义 | 影响 |
|---|---|---|
| `live_n` × `live_chars` | live 会话数与各自独立前缀长度 | **过订比** `live_tok / pool_tok` |
| `churn_n` × `churn_chars` | 外部冷流量会话与每次前缀长度 | 淘汰压力强度 |

| 数据集 | rows | infer | live 会话 × 轮 | live 前缀 | churn | 设计用途 |
|---|--:|--:|---|---:|---|---|
| `bigp_live8/12/20` | 912/1008/1200 | 816/864/960 | 8/12/20 × 12 | 24,000 字符 | 12×60 | 会话数阶梯（前缀恒定） |
| `bp25` / `bp45` / `bp70` | 912/1056/1200 | 816/888/960 | 8/14/20 × 12 | 8k/8k/9k | 12×60 | 过订比 0.25 / 0.45 / 0.69 |
| `bpdom` / `bpdl` | 1140 / 1020 | 660 / 540 | 24 × 20 | 8,000 字符 | 6×30 / 6×10 | 长会话跨度 + 轻/极轻 churn |
| `gp25` / `gp45` / `gp69` | 280/424/568 | 184/256/328 | 8/14/20 × 12 | 8k/8k/9k | 4×22 | 重 churn、少轮次 |
| `gc50` / `gc70` | 486 / 630 | 318 / 390 | 14 / 20 × 12 | 8k/9k | 5×30（10.5k 字符） | 极端 churn（容量受限） |

**形状契约**：

- live 会话：12（或 20）次 `infer`，间隔 **`HOT=28000 ms` ⇒ pause ≈ 28 s**，每次 infer 后 8 s 一个 `event` 行；
- churn 行：`kind=infer`、`session_id=churn*`、**不发 `cache_control`**（decl 臂也一样），前缀按 (会话, 轮次) 唯一 ⇒ 永不命中；
- `cache_control` 键序 `{type, ttl_s, range}`（与实测落盘件一致，保证逐字节复现）；
- `--ttl 0` 生成**只带 range、不写 `ttl_s`** 的版本 —— 实测与「完全不发 `cache_control`」在命中上逐位相同，
  且不会因 ttl < 会话跨度而提前降级 ⇒ **生产默认建议 `--ttl 0`**；
- `--strip-cc` 生成 `<name>_nocc.jsonl` 孪生：只摘 `agent_hint.cache_control`，其余逐行相同（用于
  「收益是 session_id 给的、还是 cc 字段给的」这一问）。

已核对 sha256（`--ttl 60`，与 2026-11-18 实测件**逐字节相同**）：

| 文件 | sha256 |
|---|---|
| `bp70.jsonl` | `c493b128351b903789174a52f8999961288feffacd36b180725c9ff3f0d9486e` |
| `bp25.jsonl` | `2e33706aea2d8e2dfc7852961c24063f0ae4ee671c79206c6a5398b3d51d102c` |
| `bpdom.jsonl` | `456c99ad7a53894c1510ac551cda114019be5fc65d419ab9ec683f511945981c` |
| `bigp_live12.jsonl` | `fbcb3111bd44347a80117029b2d70e5e0840283ac400e7c58170eef87ac3c6f0` |
| `gc50.jsonl` | `1d0fe6b9cb9156cd6c5334607c4631a22e98e91cadba0c25a4eb4e099f0df140` |

> 注意：这一族**不替代** sp7 族。sp7 回答「池子装得下时 hint 是加速还是开销」（结论：纯开销 +15~40 ms），
> bp/gp/gc 回答「池子过订时 hint 能兑现多少」。两者的分界在 `live_tok / pool ≥ ≈0.8`。
> 实测收益地图见 `docs/RESULTS_2026-11-18_profit_map.md`。

