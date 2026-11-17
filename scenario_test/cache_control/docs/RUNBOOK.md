# RUNBOOK：从零拉起并跑一轮

> 前置：宿主机有 Ascend 驱动（`npu-smi` 可用）、docker、两张镜像与 Qwen3-8B 权重。
> 全程命令都在 `scenario_test/cache_control/` 下执行。

## 0. 环境准备（只做一次）

```bash
make env
# 二选一：
#   vi env/env.sh                    # 从 env.example.sh 起步
#   cp env/profiles/3.2.sh env/env.sh   # 复用实测机快照（3.2 / 169）
```

`env/env.sh` 里逐项确认：

| 变量 | 说明 |
|---|---|
| `MODEL_PATH` / `SERVED_MODEL_NAME` | 模型路径与对外名（默认 `kvh`） |
| `ENGINE_IMAGE(_ID)` / `MOTOR_IMAGE(_ID)` | 镜像与**必须一致的镜像 ID** |
| `ENGINE_CONTAINER` / `MOTOR_CONTAINER` | 容器名 |
| `NPU_INDEX` / `MASTER_PORT` | 用哪张卡、vllm 内部端口 |
| `INFER_PORT` / `MGMT_PORT` / `OBS_PORT` / `ENGINE_PORT`（`WORKER_PORT`） | 端口表 |
| `SESSION_SHM_KEY` / `KVC_METRICS_SHM_NAME` | 亲和共享内存 key（多 lane 必须不同） |
| `POOL_BLOCKS_OVERRIDE` | **空=自然池**；做容量实验时填块数（169 用 2031） |

## 1. 前置检查

```bash
make preflight
```

必须全 `[ OK ]`。重点两条：

- 镜像 **ID** 一致（同名 tag 不算数）；
- **motor 容器内 `agent_hint.py` 含 `resume` 白名单**。若报「镜像原版」，先解决再跑，
  否则 resume 全被拒（症状：hit/pt 只有 43–48%）。见 `TROUBLESHOOTING.md` 第 11 条。

## 2. 数据

```bash
make data
```

生成 `data/generated/contend_sp7_p60_S{1,4,8,32}.jsonl`，并自动校验 sha256（与 `scenario.json` 对齐）。
产物不入 git。

## 3. 容器与引擎

```bash
make containers     # 幂等；已存在则跳过
make engine         # 起引擎 + 等待 /health=200 + 校验 banner
```

**必须看到的检查点**：

```
banner: 79,872 tokens = 624 blocks      # 32GB 卡自然池
# 或（169，override=2031）：
banner: 259,968 tokens = 2031 blocks
```

`02_start_engine.sh` 会把池子块数写入 `results/logs/pool.env`，`05_run_matrix.sh` 用它做容量准入。
banner 对不上（比如 32GB 卡出现 2,237 块）说明 override 没生效，**该轮作废**。

## 4. 臂隔离（必读，2026-11-17 起强制）

hints-on 流量会把引擎进程的状态改坏（会话注册表累积 / 受保护块占窗口 / 空闲计数虚高），
`reset_prefix_cache` 清不掉，**只有重启引擎进程能恢复**。所以：

- **切臂必须重启引擎**：`scripts/02_start_engine.sh --restart`（`make matrix` 默认 `per_arm` 自动做）；
- motor 也要容器级重启（`03_start_motor.sh` 默认 `docker restart`，`--no-restart` 可跳过）；
- **禁止**「先 decl 再 off 且不重启引擎」——那样测到的 off 是被劣化的基线（p50 ~1 s 级），
  `07_verify.py` 的 V9 会判 FAIL。

> **容器写操作保护**：容器名以 `ttft-` 开头时脚本默认拒绝写操作（防误伤共享机上的他人进程）。
> 确认 `ttft-*` 就是本场景容器时，在 `env/env.sh` 里设 `ALLOW_SHARED_CONTAINERS=1`（实测机 profile 已带）。
> 新机器建议用 `01_create_containers.sh` 建自己的 `cc-*` 容器、跑在空闲卡上。

> 两种形态都能跑，但要分开报：干净引擎回答「hint 是加速还是开销」；
> 混部引擎回答「hint 会话相对 off 会话的会话级优势」。见 `EXPECTED_RESULTS.md` §0。

## 5. 起网关（臂）

```bash
make motor ARM=decl      # 或 off / life
```

检查点：结尾打印 `ready=200`；`results/logs/motor-<arm>-lc<N>-<ts>.log` 里有
`central agent_status event ... applied=True`（在打流后出现）。

> `decl.json` 与 `off.json` 字节相同（md5 `a4d44f83…`），两臂唯一差别是驱动发不发请求头。
> `03_start_motor.sh` 每次会把配置复制到 `results/logs/cfg-<arm>-<ts>.json` 再注入
> 模型路径/端口/SHM 名，原配置保持不动，便于审计。

## 6. 打流

```bash
make run DS=contend_sp7_p60_S8.jsonl ARM=decl
# 等价于
scripts/04_run_one.sh contend_sp7_p60_S8.jsonl decl 1 A
```

单轮 span：S8 ≈ 540 s、S4 ≈ 508 s、S32 ≈ 732 s（由数据节奏决定，不是吞吐）。
证据落到 `results/<dataset>_<arm>_<ts>/`，四个文件：
`meta.json`、`requests.jsonl`、`metrics_before.json`、`metrics_after.json`。

## 7. 验收

```bash
make analyze
make verify            # 或 scripts/07_verify.py --latest --arm decl
```

逐条看 V1–**V9**（见 `EXPECTED_RESULTS.md`）。任何一条 FAIL ⇒ 该轮作废，不要拿来对比。
V9 专门拦「off 轮跑在跑过 hint 的引擎实例上」——这是最隐蔽的一类废轮。

## 8. 跑完整对照

```bash
make matrix            # 默认 S8 × decl/off × 3 轮
# 或在 config/scenario.json 里改 matrix；先 make matrix ARGS=--dry-run 看计划
```

`05_run_matrix.sh` 会：校验数据 sha → 容量准入 → 每轮重启 motor → 打流 → 单轮验收。

## 单轮耗时与排队

- 每轮 ~9–12 分钟（含 60 s pause × 8 次）。建议按 lifetime 规划，不要中途 Ctrl-C
  （会留下无 `meta.json` 的半截证据目录，`06_analyze.py` 会自动忽略）。
- 同一 lane 一次只跑一轮；lane B 与 lane A 必须用不同的端口与 `SESSION_SHM_KEY`。
