# KVC 管理功能手工测试（AISBench + Motor + vLLM）

本套件用于**单卡 TP1、关闭会话亲和路由**的条件下，手工验证 Agent Hint KVC 管理链路：

```text
pause -> offload -> resume -> prefetch
finalize -> evict
high-water gate / KVC-off 对照
```

本轮执行结果见：`kvc_manual_execution_report_20261105.md`。
最近一次 fast 全量报告：`results/QA_REPORT_latest.html`（6/6 PASS）。

---

## 0. 交付到新服务器（推荐）

本套件已与开发机解耦：镜像、路径、端口、卡号、模型名全部来自 `kvc_test.env`。
目标机执行：

```bash
tar xzf kvc_manual_test_<version>.tar.gz
cd kvc_manual_test

./qa_run.sh init          # 生成 kvc_test.env
vi kvc_test.env           # 至少改 MODEL_PATH / MODEL_NAME / 镜像 / CARD
./qa_run.sh doctor        # 环境、镜像、模型、端口、卡、版本检查

./qa_run.sh all --fast    # 一键跑 S1/S2/S3/S5/S6/S7
# 打开 results/QA_REPORT_latest.html
```

交付物清单、镜像 `docker save/load`、目标机前置条件见 `DELIVERY.md`。

## 1. 配置契约

唯一配置入口是 `kvc_test.env`（从 `kvc_test.env.example` 复制）。加载优先级：

```text
显式环境变量 > KVC_TEST_ENV 指定的文件 > ./kvc_test.env > 内置默认值
```

关键字段：

| 字段 | 说明 |
|---|---|
| `BENCH_SOURCE_DIR` | benchmark 源码目录（提供 AISBench v2 代码） |
| `MODEL_PATH` / `TOKENIZER_PATH` / `MODEL_NAME` | 模型路径、tokenizer、服务名 |
| `MAX_MODEL_LEN` / `CHAT_API` | 上下文长度；是否使用 chat/completions |
| `CARD` / `CARDS` / `TP_SIZE` | 单卡或 TP 配置 |
| `ENGINE_IMAGE` / `MOTOR_IMAGE` / `AIS_IMAGE` | 三个交付镜像 |
| `MOTOR_INSTALL_MODE` | `image` / `wheel` / `source` |
| `WORK_DIR` / `DATA_DIR` / `RESULTS_DIR` | 运行、数据、结果目录 |
| `ENGINE_PORT` / `MOTOR_*_PORT` / `MMC_*_PORT` | 全部端口 |
| `KVC_DATA_MODE` | `synthetic` / `legacy` / `external` |
| `ENGINE_START_TIMEOUT` | engine 启动超时（默认 300s） |

完整注释见 `kvc_test.env.example`。

## 2. 数据与 Motor 模式

### 数据模式

| 模式 | 说明 | 是否需要外部数据 |
|---|---|---|
| `synthetic` | 用模型 tokenizer 现场生成确定性长 prompt（推荐） | 否 |
| `legacy` | 使用 `SOURCE_DATA_DIR` 下的 SWE-bench 派生 JSONL | 是 |
| `external` | 使用 `DATA_DIR` 下已存在的同名 JSONL | 是 |

### Motor 安装模式

| 模式 | 说明 |
|---|---|
| `image` | 使用交付的 Motor 镜像（推荐） |
| `wheel` | 安装 `MOTOR_WHEEL` |
| `source` | 从 `MOTOR_SOURCE_DIR` 构建（先 `./qa_run.sh prepare-motor`） |

## 3. 隔离约定

本套件使用独立容器、端口、卡和运行目录，不操作性能测试栈：

| 项 | 取值 |
|---|---|
| 容器 | `${KVC_CONTAINER_PREFIX}-motor` / `-engine-0` / `-memcache-meta` |
| NPU | `CARD`（默认 0）或 `CARDS` |
| engine | `${HOST_IP}:${ENGINE_PORT}`，TP1 |
| Motor | infer / mgmt / obs 三个配置端口 |
| MemCache | meta / store / metrics / lookup 四个配置端口 |
| 运行目录 | `$WORK_DIR` |
| 数据目录 | `$DATA_DIR` |
| 结果目录 | `$RESULTS_DIR` |

engine 固定使用 `--ipc private`，避免与性能栈的 `/dev/shm/memfabric_hybrid_*` 冲突。
Motor 的 affinity/metrics shm key 也来自配置，默认带独立前缀。

### 会话亲和

KVC 需要 Motor 记录“哪个 engine 持有该 session”的 location pin，因此**不能**设置
`MOTOR_SESSION_AFFINITY=0`。本套件固定：

```bash
MOTOR_SESSION_AFFINITY=1
MOTOR_SESSION_AFFINITY_SHARED=1
MOTOR_SESSION_AFFINITY_PLACEMENT=balanced
MOTOR_SESSION_AFFINITY_ROUTE=0
```

`ROUTE=0` 表示路由完全走负载均衡，不使用亲和；pin 只作为 KVC 定位账本。

## 3.1 目录结构

```text
kvc_manual_test/
├── README.md
├── DELIVERY.md
├── kvc_test.env.example
├── qa_run.sh
├── configs/legacy/            # 旧静态配置，仅用于本地对照
├── docker/
│   └── Dockerfile.ais-runner  # AIS 镜像（ais-runner:ais-1）构建文件
├── scripts/
│   ├── common.sh              # 配置加载
│   ├── preflight.sh           # doctor
│   ├── render_ais_configs.py  # 运行时生成 AISBench 配置
│   ├── make_synthetic_datasets.py
│   ├── build_datasets.sh
│   ├── start_stack.sh / stop_stack.sh
│   ├── sample_metrics.py / observe.py
│   ├── run_case.sh / check_case.py
│   └── make_qa_report.py
├── cases/
└── results/
```

## 4. 生成数据集

```bash
cd kvc_manual_test
./qa_run.sh datasets --fast
```

`synthetic` 模式会同时生成 fast 和 standard 两套数据；`legacy` / `external` 模式见
`DELIVERY.md`。生成后用模型 tokenizer 校验
`prompt + max_out_len + safety_margin <= MAX_MODEL_LEN`，并清理 `tools=[]` 时残留的
`tool_choice`。

固定文件名（AISBench 配置按名称引用）：

| 文件 | 用途 |
|---|---|
| `kvc_manual_pause_offload_functional*.jsonl` | S3/S4 pause→offload→prefetch |
| `kvc_manual_finalize_quick*.jsonl` | S2 finalize→evict |
| `kvc_manual_gate_quick*.jsonl` | S6/S7 对照与 gate |
| `kvc_manual_pause_offload_quick*.jsonl` | S1/S5 压满基础数据 |
| `kvc_manual_saturation_pause*.jsonl` | S5 早 pause 压满 |
| `kvc_manual_saturation*.jsonl` | 压满基线备用 |

精确行数/session 数/token 统计见各 `*.jsonl.report.json`。

---

## 5. 启动 / 停止隔离栈

### 首次启动（含 MemCache + engine）

```bash
cd kvc_manual_test

# KVC on：pause->offload、resume->prefetch、finalize->evict
bash scripts/start_stack.sh \
  --kvc on \
  --idle-offload 30 --idle-evict 0 --high-water 0 --prefetch on
```

### 只重启 Motor（engine/MemCache 复用）

**每个 KVC-on 场景之间都必须重启 Motor**，因为 `finalize` 是终态、session_id 会复用：

```bash
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 0 --prefetch on \
  --no-engine --no-meta
```

### 停止

```bash
bash scripts/stop_stack.sh
```

---

## 6. 场景执行

一键串跑全部场景（fast 约 25–35 分钟，结束后自动停栈）：

```bash
cd kvc_manual_test
./qa_run.sh all --fast
# 想保留栈：KEEP_STACK=1 ./qa_run.sh all --fast
```

手工逐项验证时，所有场景统一用 `run_case.sh`：

```bash
bash scripts/run_case.sh \
  --case <case-name> --model <model-config> --dataset <dataset-config> [--duration SEC]
```

`--duration` 到期后会停止 AISBench 容器，适合压满观察；不传则等数据集跑完。

### S1 压满基线（KVC off）

目的：确认 c=8 能压满，记录 KV usage / Waiting。

```bash
bash scripts/start_stack.sh \
  --kvc off --no-engine --no-meta

bash scripts/run_case.sh \
  --case ramp_kvc_off_pause_c8 \
  --model kvc_manual_c8 \
  --dataset kvc_manual_pause_offload_multi \
  --duration 180
```

通过判据：`KV usage max >= 70%` 或 `Waiting > 0` 或 preemption 增长。

### S2 finalize → evict

```bash
bash scripts/start_stack.sh \
  --kvc on --idle-offload 0 --high-water 0 --prefetch off \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case finalize_evict \
  --model kvc_manual_c8 \
  --dataset kvc_manual_finalize_quick
```

通过判据：

- `evict_total > 0`，且与 `state=finalize` 的 POST 数一致；
- `offload_total = prefetch_total = 0`；
- `edit_fail=0`、`send_fail=0`；
- engine 日志有 `Executing edit type=evict` 且 `actual_op_block_num > 0`。

### S3/S4 pause → offload → resume → prefetch

```bash
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 0 --prefetch on \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case pause_offload_prefetch \
  --model kvc_manual_c8 \
  --dataset kvc_manual_pause_offload_quick
```

通过判据：

- `offload_total > 0`，且与 pause POST 数匹配；
- `prefetch_total > 0` 且 `<= offload_total`；
- `edit_fail=0`、`send_fail=0`；
- engine 日志有 offload / prefetch 编辑和 block 数。

> `prefetch` 是异步提交，`prefetch_total>0` 只代表已提交，不代表数据已回 HBM。

### S5 压满触发（high_water=0.7）

```bash
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 0.7 --prefetch on \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case saturated \
  --model kvc_manual_c8 \
  --dataset kvc_manual_saturation_pause \
  --duration 300
```

通过判据：

- 采样到 `KV usage max >= 70%` 或 `Waiting > 0`；
- `offload_total > 0`，`edit_fail=0`；
- `gate_skip` 可 >0（offload 本身会降 usage，出现自限是正常现象）。

### S6 KVC off 对照

```bash
bash scripts/start_stack.sh \
  --kvc off --no-engine --no-meta

bash scripts/run_case.sh \
  --case kvc_off \
  --model kvc_manual_c1 \
  --dataset kvc_manual_gate_quick
```

通过判据：

- AISBench 仍出现 pause / running / finalize 的 `/agent-status` POST，且 HTTP 200；
- `motor:kvc_*` 全部不增长；
- engine 无任何 edit；
- 请求成功率 100%。

### S7 high-water gate

```bash
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 1.0 --prefetch on \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case gate \
  --model kvc_manual_c1 \
  --dataset kvc_manual_gate_quick
```

通过判据：

- `gate_skip_total > 0`；
- `offload_total = 0`、`evict_total = 0`；
- `edit_fail=0`。

---

## 7. 如何看结果

每个 case 在 `results/<时间戳>_<case>/` 下产出：

```text
aisbench/                 # AISBench 原始输出（details/failed/json）
motor-before.prom         # 运行前 Motor KVC 指标
motor-after.prom          # 运行后 Motor KVC 指标
engine-metrics.csv        # 1s 采样：KV usage / waiting / preemption / kvc 计数
aisbench.log              # 容器 stdout
coordinator-kvc.log       # 过滤后的 Motor KVC 日志
engine-kvc.log            # 过滤后的 engine edit 日志
summary.json / summary.txt# 自动判据与汇总
run_rc.txt                # AISBench 退出码（137 = 到时被停止）
```

快速查看：

```bash
cat results/<dir>/summary.txt
```

关键日志：

```bash
grep -E 'kvc (offload|prefetch|evict) done|manage dispatch' results/<dir>/coordinator-kvc.log
grep -E 'Executing edit|actual_op_block_num' results/<dir>/engine-kvc.log
```

---

## 8. 常见问题

1. **同一 session 的 pause 返回 `applied=false reason=terminal`**
   上一轮已经发过 finalize。**每个 KVC-on 场景前重启 Motor**，或使用新的 session_id。

2. **`kvc offload skipped: no session pin`**
   会话亲和被完全关闭了。保留 `MOTOR_SESSION_AFFINITY=1`，只设置 `ROUTE=0`。

3. **`gate_skip` 增加但 `offload_total` 不涨**
   KV usage 未到 `high_water`，或 offload 后 usage 降下去导致后续被 gate。功能验证请用 `--high-water 0`。

4. **engine 启动报 `/dev/shm/memfabric_hybrid_0 already in use`**
   本套件 engine 必须使用 `--ipc private`；确认没有被改回 `--ipc host`。

5. **请求 400：`maximum context length`**
   手工改数据后未做 tokenizer 校验。运行 `build_datasets.sh` 或
   `filter_dataset_context.py`，安全余量默认 256 token。

6. **请求 400：`When using tool_choice, tools must be set`**
   `tools=[]` 时残留了 `tool_choice`。上下文过滤器会把这种情况的 `tool_choice` 置空。

7. **AISBench 段错误**
   容器内先执行 `pip uninstall -y av`；`run_case.sh` 已固化。

8. **`--no-engine` 启动后 Motor 没有可用 engine**
   `start_stack.sh` 会检查 `18000/v1/models`，失败直接报错；先启动 engine 或去掉 `--no-engine`。

---

## 9. 与本轮执行结果的对应

### 9.1 解耦验证（模拟新服务器，2026-11-07）

配置：`WORK_DIR=/tmp/kvc_decouple`、卡 6、engine `18100`、Motor `19225-19227`、
模型名 `new-kvh`、`KVC_DATA_MODE=synthetic`。结果：`all --fast` **6/6 PASS**。

| 场景 | 结果 | 关键观测 |
|---|---|---|
| S1 | PASS | KV p50 68.5% / max 74.6%，Waiting 7 |
| S2 | PASS | 4 finalize → 4 evict / 360 blocks |
| S3 | PASS | 3 pause → 3 offload → 3 prefetch，3 evict |
| S5 | PASS | KV p50 80.6% / max 98.7%，10 offload / 8 prefetch |
| S6 | PASS | 生命周期正常，KVC/engine edit 全 0 |
| S7 | PASS | gate_skip=2，offload=0 |

示例报告：`QA_REPORT_sample.md`。详细交付步骤见 `DELIVERY.md`。

### 9.2 开发机标准模式历史证据

| 场景 | 结果目录 | 结论 |
|---|---|---|
| S1 ramp | `results/20261105_215533_ramp_kvc_off_pause_c8` | Pass：KV p50 72.4%，max 97.4% |
| S2 finalize→evict | `results/20261105_215909_finalize_evict` | Pass：6 finalize → 6 evict / 985 blocks |
| S3/S4 pause→offload→prefetch | `results/20261105_231030_pause_offload_prefetch` | Pass：5 pause → 5 offload → 5 prefetch，4 evict |
| S5 压满触发 | `results/20261105_233251_saturated` | Pass：KV p50 95.7%，8 offload / 6 prefetch |
| S6 KVC off | `results/20261105_233822_kvc_off` | Pass：生命周期正常，KVC/engine 动作全 0 |
| S7 high-water gate | `results/20261105_234447_gate` | Pass：gate_skip=2，offload=0 |

详见 `kvc_manual_execution_report_20261105.md`。
