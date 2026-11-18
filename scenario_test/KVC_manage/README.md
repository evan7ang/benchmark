# KVC_manage 场景测试（长 pause + KVC lifecycle）

验证长 pause（180s）后 KV 被后续请求挤出 HBM 时，`cache_control` + KVC lifecycle 的
`pause→offload`、`resume→prefetch` 是否能恢复 prefix cache 并降低 TTFT。

本目录是 **AISBench** 版本，不是短 pause 的 `replay_driver` 版本。  
最终口径：**overall mean TTFT** 为主指标；overall p50、victim resume TTFT、
prefix cache 命中率作为辅助/机制指标。

## 目录

```text
KVC_manage/
├── env/                  环境契约与 3.2/168 profiles
├── config/               scenario.json + motor/off.json + motor/on.json
├── data/                 source + 离线生成器 + generated 数据集
├── ais_bench/            AISBench dataset/model configs
├── scripts/              preflight / engine / motor / run / analyze
├── docs/                 DESIGN / RUNBOOK / EXPECTED_RESULTS / TROUBLESHOOTING
└── results/              参考结果摘要
```

## 臂

| 臂 | header/cache_control | lifecycle | offload | prefetch |
|---|:--:|:--:|:--:|:--:|
| `off` | 否 | 关 | 否 | 否 |
| `on` | 是 | 开 | 是 | 是 |

主对比是 `off` vs `on`。`on` 的 Motor 配置使用 `idle_offload_s=1`、`idle_evict_s=0`、
`prefetch_enabled=true`。

## 参考结果

16 victims + 4 attacker sessions，P180，1600 blocks：

| 指标 | off | on |
|---|---:|---:|
| overall mean TTFT | 2442.5 ms | **1800.1 ms（−26.3%）** |
| overall p50（背景） | 1139.8 ms | 1083.0 ms |
| victim resume p50 | 5010.7 ms | **272.9 ms（−94.6%）** |
| attacker p50 | 1140.8 ms | 1166.5 ms |
| victim resume hit | ~0 | **65/66 blocks（98.4% token）** |

prefix 交叉验证：

```text
on hits - off hits = 133,120 token = 16 × 65 × 128
```

## 快速开始

```bash
cd benchmark/scenario_test/KVC_manage
cp env/profiles/3.2.sh env/env.sh
vi env/env.sh                  # 按本机改路径/容器/端口/卡

bash scripts/00_preflight.sh
bash scripts/02_start_engine.sh off
bash scripts/03_start_motor.sh off
bash scripts/04_run_one.sh off
# 重新起引擎和 Motor 后再跑 on
```

`scripts/05_run_matrix.sh` 会按臂重启引擎和 Motor 后依次跑 `off` / `on`。

## 外部依赖

- `agenthint-vllm:0.1.0-p3memcache`
- `mindie-motor-vllm:e2e3b`
- `ais-runner:ais-1`
- `benchmark-ah` 的 AISBench，包含修复 `resume -> running` 的提交 `c199768`
- Qwen3-8B `/home/RAG_data/models/Qwen3-8B`

## 已知边界

- `edit_fail` 会包含 attacker 的 4 个初始 pause 行；不影响 victim 的 16 次
  offload/prefetch。
- control 没有 header/session，无法从引擎日志按 victim 过滤；victim 命中率由
  on/off 的 prefix hits 差值交叉验证。
- 更完整的分析见 `docs/EXPECTED_RESULTS.md`。
