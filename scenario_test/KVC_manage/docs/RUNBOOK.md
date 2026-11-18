# RUNBOOK

## 0. 环境

```bash
cd benchmark/scenario_test/KVC_manage
cp env/profiles/3.2.sh env/env.sh
vi env/env.sh
```

必须保证：

- `AIS_BENCH_DIR` 指向含 `c199768` resume 修复的 benchmark-ah
- `AGENT_HINT` 引擎镜像和 Motor 镜像已加载
- 1600 blocks 能启动（banner `204,800 tokens`）
- 每次切臂前重启引擎和 Motor

## 1. 预检

```bash
bash scripts/00_preflight.sh
```

## 2. 生成数据

```bash
python3 data/gen_lpstd.py --out data/generated/lpstd_large_v16_a4x16.jsonl
```

## 3. 单臂

```bash
bash scripts/02_start_engine.sh off
bash scripts/03_start_motor.sh off
bash scripts/04_run_one.sh off
```

再重启引擎/Motor 跑 `on`。

## 4. 矩阵

```bash
bash scripts/05_run_matrix.sh
```

每个臂自动重启引擎/Motor，避免 HBM 和 prefix cache 残留。

## 5. 分析

```bash
python3 scripts/05_analyze.py results/<arm-dir>
```

输出 victim/attacker/overall TTFT、prefix cache 差分和 `cached_before`。
