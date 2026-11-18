# cache_control 场景测试（短 pause + agent hint）

> **三种形态，结论不可混说**（详见 `docs/EXPECTED_RESULTS.md` §0）：
> | 形态 | 做什么 | 回答的问题 | 判据 |
> |---|---|---|---|
> | **clean** | 每臂前重启引擎 | hint 本身是加速还是开销 | V1–V9 |
> | **churn** | 干净引擎 + 受控外部冷流量（churn） | 共享引擎有淘汰压力时 hint 能否保住本会话前缀（**正收益主证据**） | V1–V11 |
> | **mixed** | 同引擎先跑 decl、不重启接 off | 生产混部形态下 hint 会话的会话级优势（参考） | V9 预期 FAIL，仅归档 |

验证 **agent hint（请求头 `X-Agent-Hint` 里的 `cache_control`）** 在「短 pause（60 s）→
resume」场景下能不能把 TTFT p50 拉下来：pause 期间保护会话前缀的 KV 块，resume 回来直接命中，
跳过 8.4 k token 的整段 prefill。

**结论必须分两种形态说**（2026-11-17 实测更正）：

| 形态 | 结论 |
|---|---|
| **干净引擎**（每臂前重启） | 池子装得下时，off 本来就能活过 60 s pause（p50 ~186–200 ms）；hint 是**纯开销 +15–20 ms** |
| **混部引擎**（同一引擎先跑过 hint） | off 基线被引擎状态污染到 ~1020–1028 ms；hint 会话 p50 ~197–220 ms ⇒ **会话级优势 ~80%** |

`reset_prefix_cache` 清不掉 hint 留下的引擎状态（会话注册表/受保护块/虚高空闲计数），
**切臂必须重启引擎进程**。本包用 `V9 基线洁净度` 判据强制拦住被污染的 off 轮。
详见 `docs/EXPECTED_RESULTS.md` §0 与 `docs/DESIGN.md` §5。

**本包已在本机（3.2，NPU2 隔离栈）完成干净引擎双跑验证**：
`off p50 186.2 ms` / `decl p50 218.7 ms`（hint +32.5 ms 纯开销），V1–V9 全 PASS。
验证记录见 `docs/EXPECTED_RESULTS.md` §6。

---

## 目录

```
cache_control/
├── env/                  唯一环境契约（env.example.sh + 各机 profiles/）
├── config/               scenario.json（场景参数/矩阵）+ motor/{decl,off,life}.json
├── data/                 冻结源 + 离线生成器（产物 generated/ 不入 git）
├── driver/               replay_driver.py（vendored，路径/端口/容器参数化）
├── scripts/              00 preflight … 07 verify
├── docs/                 DESIGN / RUNBOOK / EXPECTED_RESULTS / TROUBLESHOOTING
└── results/              证据与日志（不入 git）
```

## 快速开始

```bash
cd benchmark/scenario_test/cache_control

make env                      # 生成 env/env.sh（或 cp env/profiles/3.2.sh env/env.sh）
vi env/env.sh                 # 按本机改：模型路径 / 镜像 ID / 容器名 / 端口 / NPU / 池子
make preflight                # 全部 OK 再继续（会校验 motor 代码是否含 resume 白名单）
make data                     # 生成 S1/S4/S8/S32 数据集（校验 sha256）
make containers               # 幂等建容器（已存在则跳过）
MOTOR_PATCH_SRC=<补丁源> scripts/00b_sync_motor_patch.sh   # stock 镜像必做（见 TROUBLESHOOTING §18）
make engine                   # 起引擎，校验 banner，写 results/logs/pool.env

# ---- 形态 A（clean）：干净引擎，最小对照 ----
make matrix                    # S8 off/decl 各 1 轮，每臂前重启引擎

# ---- 形态 C（churn）：正收益主证据 ----
make data                      # 生成 S*/L* 数据集（含 churn）
make matrix-churn              # S8 + churn L2（≈22.7 块/s）off×2 / decl×2
make matrix-churn-all          # L1/L2/L3 三档压力扫描

# ---- 形态 B（mixed）：生产混部参考（V9 预期 FAIL）----
make matrix-mixed

# ---- 手工单轮（任意形态）----
scripts/02_start_engine.sh --restart
make motor ARM=decl
AH_FORM=churn scripts/04_run_one.sh contend_sp7churn_p60_S8_L2.jsonl decl 1 A

make analyze                  # agent 组 p50/mean/le500/Δhits/Δprefill/恒等式/压力
make verify                   # V1–V11 逐条 PASS/FAIL/SKIP
```

## 选哪个规模（先算容量，再决定）

**准入规则：`池子块数 ≥ 1.05 × 会话数 × 66`**（每会话工作集 66 块）。

| 卡 | 自然池 | 本机可复现正收益的规模 | 备注 |
|---|--:|---|---|
| 910B4 32 GB（3.2/168） | 624 块 | **S8（528 块，比值 0.85）**、S4（0.42） | S32 需求 3.4 倍池子 ⇒ 收益归零 |
| 910B4-1 64 GB（169） | 2,237 块（压到 2,031） | **S32（2,112 块，比值 1.04）** | 勉强装下；需 `POOL_BLOCKS_OVERRIDE=2031` |

> 换卡时按 `KV_GiB ÷ 18.0 MB` 估算池子块数。比值 > 1.05 时 `05_run_matrix.sh` 会拒绝执行（`--force` 可强跑）。

## 三种形态（form）

形态由 `config/scenario.json -> forms` 定义，`05_run_matrix.sh --matrix <集>` 选择。
`AH_FORM` 环境变量会写进 `_run_meta.json`，`07_verify.py` 据此启用 V9/V10/V11。

| form | 引擎隔离 | V9 | V10/V11 | 用途 |
|---|---|---|---|---|
| `clean` | 每臂重启 | 强制 | SKIP | 干净基线（无外部压力） |
| `churn` | **每轮**重启 | 强制 | **强制** | 正收益主证据 |
| `mixed` | 刻意不重启 | 预期 FAIL | SKIP | 生产混部参考 |

## 三条臂

| 臂 | 发 `X-Agent-Hint` | lifecycle | 用途 |
|---|:--:|:--:|---|
| `decl` | ✅ | 关 | **交付配置**（hint only） |
| `off` | ❌ | 关 | 对照组（与 decl 配置字节相同，只差请求头） |
| `life` | ✅ | 开 | 可选对照（同 cached 下只加开销） |

## 产物校验（数据）

`data/generated/` 不入 git，由 `make data` 生成。四份数据 sha256：

| 文件 | sha256 |
|---|---|
| `contend_sp7_p60_S1.jsonl` | `8efda7a7…ae1346` |
| `contend_sp7_p60_S4.jsonl` | `039eabcb…f8c292` |
| `contend_sp7_p60_S8.jsonl` | `d509d433…b9d549` |
| `contend_sp7_p60_S32.jsonl` | `994336a5…e7d9f9f`（完整值见 `config/scenario.json`） |
| `contend_sp7churn_p60_S8_L1.jsonl` | `b15b0ac6…a455cc288c`（churn 2.0 s ≈11.3 块/s） |
| `contend_sp7churn_p60_S8_L2.jsonl` | `acad6b09…324483ccc`（churn 1.0 s ≈22.7 块/s，推荐） |
| `contend_sp7churn_p60_S8_L3.jsonl` | `93661d61…a00838841c`（churn 0.7 s ≈32.4 块/s） |

churn 数据集 = agent 行（与无 churn 版**逐字节相同**）+ 一次性冷流量行（`session_id=churn-*`、
无 `cache_control`、永不复用前缀）。它只制造块淘汰压力，不占保护额度。

## 外部依赖（不入库，preflight 校验）

- 引擎镜像 `agenthint-vllm:0.1.0-p3memcache`（ID `sha256:de6b0167…`）
- 网关镜像 `mindie-motor-vllm:e2e3b`（ID `sha256:d7889635…`）——**容器内的 motor 代码必须含
  `resume` 状态白名单**（否则 resume 被拒、命中率腰斩，见 `docs/TROUBLESHOOTING.md` 第 11 条）
- 模型 `Qwen3-8B`（`--served-model-name kvh`）

## churn 形态怎么判读（拿到结果先看这几列）

`make analyze` 的输出里：

- `agP50` / `agMean` / `le500`：**agent 组**（收益看这个，churn 组不参与）
- `churn50`：压力源自己的延迟（应该稳定在 300–400 ms；暴涨说明引擎过载）
- `reuse`：churn 形态下应 ≈3,400–3,560 块（理论上限 = resume 数 × 66）
- `press`：实测压力（块/秒）——由 churn 实耗 token 反算，不要用假设值
- `ident`：恒等式；churn 形态允许 ±0.05% 采样竞态容差（输出会带 `(+N)`）

验收看 `make verify` 的 **V10（off 的 agent hit/pt ≤ 5%，证明压力有效）** 与
**V11（decl 的 resume p50 ≤ 0.5× 配对 off）**。这两条不成立时，收益结论不成立。

## 扩展

- **换规模**：改 `config/scenario.json` 的 `matrix`（并确认容量准入）。
- **加臂**：`config/motor/` 放配置 + 扩 `03_start_motor.sh` 的 arm→ENVLC 映射。
- **换机**：新增 `env/profiles/<host>.sh`，`cp` 成 `env/env.sh` 即可；其余文件不动。
