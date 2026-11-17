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
