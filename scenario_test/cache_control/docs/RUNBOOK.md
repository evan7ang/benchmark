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

### 2.1 同步 motor 补丁包（stock 镜像必做一次）

```bash
make containers
MOTOR_PATCH_SRC=longpause-motor scripts/00b_sync_motor_patch.sh
make preflight     # 应显示 resume 白名单 + /internal/agent-status + grpcio>=1.84.1
```

> 只在容器重建后需要重做；`make containers` 建的是 stock 镜像，缺补丁。

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
- **重启必须杀干净**：`pkill -9 -f '[V]LLM'`（vLLM v1 的 `VLLM::EngineCore` 会逃过
  `pkill 'vllm serve'`）+ 等 HBM 释放 + 校验 `:18220` 计数器≈0。`02_start_engine.sh` 已内置；
- motor 也要容器级重启（`03_start_motor.sh` 默认 `docker restart`，`--no-restart` 可跳过）；
- **禁止**「先 decl 再 off 且不重启引擎」——那样测到的 off 是被劣化的基线（p50 ~1 s 级），
  `07_verify.py` 的 V9 会判 FAIL。

> **容器写操作保护**：容器名以 `ttft-` 开头时脚本默认拒绝写操作（防误伤共享机上的他人进程）。
> 确认 `ttft-*` 就是本场景容器时，在 `env/env.sh` 里设 `ALLOW_SHARED_CONTAINERS=1`（实测机 profile 已带）。
> 新机器建议用 `01_create_containers.sh` 建自己的 `cc-*` 容器、跑在空闲卡上。

> 三种形态都能跑，但要分开报（见 `EXPECTED_RESULTS.md` §0）：
> clean 回答「hint 是加速还是开销」；churn 回答「有淘汰压力时 hint 能否保住前缀」（正收益主证据）；
> mixed 回答「生产混部形态下 hint 会话的会话级优势」（V9 预期 FAIL，仅归档）。

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

逐条看 V1–**V11**（见 `EXPECTED_RESULTS.md` §1）。任何一条 FAIL ⇒ 该轮作废，不要拿来对比。
- V9 拦「off 轮跑在跑过 hint 的引擎实例上」（三元组 pid+started_ts+image_id）；
- V10 拦「churn 太弱、off 也能命中」（off 的 agent hit/pt 必须 ≤5%）；
- V11 拦「跑了但没效果」（decl resume p50 必须 ≤0.5× 配对 off）。

## 8. 跑矩阵（三种形态）

```bash
# 形态 A：干净引擎最小对照（S8 off/decl 各 1 轮）
make matrix

# 形态 C：正收益主证据（S8 + churn L2，off×2 / decl×2）；约 55 min
make matrix-churn

# 形态 C：三档压力扫描（L1/L2/L3 各 off/decl 1 轮）；约 2.2 h
make matrix-churn-all

# 形态 B：生产混部参考（decl 后不重启接 off；V9 预期 FAIL）；约 26 min
make matrix-mixed

# 先看计划 / 忽略容量准入
bash scripts/05_run_matrix.sh --matrix churn --dry-run
bash scripts/05_run_matrix.sh --matrix churn-all --force
```

`05_run_matrix.sh` 会：校验数据 sha → 容量准入（churn 数据集按 **agent 会话数**算需求）
→ 按形态决定是否重启引擎 → 每轮重启 motor → 打流 → 单轮验收（带 `--form`，启用 V9/V10/V11）。

## 8.1 churn 数据集怎么来的

```bash
make data   # = gen_sp7.py --sessions 1,4,8,32 --churn-intervals-ms 2000,1000,700
```

产出 `contend_sp7churn_p60_S<N>_L{1,2,3}.jsonl`：agent 行与无 churn 版**逐字节相同**，
另加 `churn-*` 一次性冷流量（无 `cache_control`、前缀永不重复）。详见 `data/README.md`。

## 单轮耗时与排队

- 每轮 ~9–12 分钟（含 60 s pause × 8 次）。建议按 lifetime 规划，不要中途 Ctrl-C
  （会留下无 `meta.json` 的半截证据目录，`06_analyze.py` 会自动忽略）。
- 同一 lane 一次只跑一轮；lane B 与 lane A 必须用不同的端口与 `SESSION_SHM_KEY`。
