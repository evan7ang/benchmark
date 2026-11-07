> 说明：本文是 2026-11-05 开发机上的历史执行报告。
> 当前交付/新服务器流程以 `README.md` 和 `DELIVERY.md` 为准；
> 解耦后的 6/6 验证结果见 `DELIVERY.md` 第 7 节和 `QA_REPORT_sample.md`。

# KVC 管理功能测试执行报告（2026-11-05）

> 范围：单卡 TP1、关闭会话亲和路由（location-only pin）下的 KVC 管理功能验证。
> 工具：AISBench Agent Hint V2 Header 数据集 + `/agent-status`。
> 套件：`benchmark/tests/manual/kvc_management/`。
> 隔离：`kvcfunc-*` 容器 / 卡 1 / engine `18000` / Motor `19125-19127`；
> 未操作性能测试 `agentx-*` 实例。

## 1. 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-11-05 |
| NPU | 910B4，单卡（卡 1），TP1 |
| 模型 | `/home/RAG_data/models/Qwen3-8B`，`max_model_len=32768` |
| engine | `agenthint-vllm:0.1.0-p3memcache` |
| Motor | `mindie-motor-vllm:e2e3b` + `motor-wheel-kvc-func/motor-3.1.0-py3-none-linux_aarch64.whl` |
| AISBench | `ais-runner:ais-1`，`PYTHONPATH=/workspace` 使用仓库源码 |
| KV 池 | AscendStoreConnector + MemCache，`NUM_CACHE_LEVELS=2` |
| 亲和 | `MOTOR_SESSION_AFFINITY=1`、`SHARED=1`、`PLACEMENT=balanced`、`ROUTE=0` |
| Motor KVC | `agent_status_config.central_lifecycle=true`、`kvc_lifecycle_config.enabled=true` |

说明：KVC 的 manage 请求依赖 session location pin；完全关闭 `MOTOR_SESSION_AFFINITY=0`
会得到 `no session pin`。因此本测试使用 `ROUTE=0`：**路由不亲和，只保留定位账本**。

## 2. 结论总览

| 场景 | 结论 | 关键结果 |
|---|---|---|
| S1 压满基线（KVC off） | **Pass** | KV usage p50 72.4% / max 97.4%，Waiting max 6 |
| S2 finalize → evict | **Pass** | 6 finalize → 6 evict / 985 blocks，`edit_fail=0` |
| S3/S4 pause → offload → prefetch | **Pass** | 5 pause → 5 offload → 5 prefetch，4 finalize → 4 evict |
| S5 压满触发（high_water=0.7） | **Pass** | KV p50 95.7% / max 100%，8 offload / 6 prefetch |
| S6 KVC off 对照 | **Pass** | `/agent-status` 正常，KVC 计数与 engine edit 全 0 |
| S7 high-water gate | **Pass** | `gate_skip=2`，`offload=0`、`evict=0` |

补充参考（性能栈上的完整数据集，不属于本隔离套件）：324 请求全部成功，
`offload=33`、`prefetch=29`、`evict=41`、`edit_fail=0`、`send_fail=0`；
说明完整 pause/offload 数据集链路也可稳定触发。

## 3. 逐场景证据

### S1 压满基线（KVC off）

```text
命令: run_case.sh --case ramp_kvc_off_pause_c8 --model kvc_manual_c8 \
       --dataset kvc_manual_pause_offload_quick --duration 180
证据: results/20261105_215533_ramp_kvc_off_pause_c8/
```

- 采样：KV usage p50 **72.4%**、max **97.4%**；Waiting max **6**；preemption delta 0。
- AISBench 在 180s 到时停止（exit 137），只看压满指标，不要求请求全部完成。
- 补充：更早一次不带 duration 的 c8 运行采样到 KV p50 92.0%、max 100%、Waiting 7、
  preemption 6；该次因旧数据集未做 tokenizer 校验出现 4 个 400，已由上下文过滤器修复。

### S2 finalize → evict

```text
命令: start_stack.sh --kvc on --idle-offload 0 --high-water 0 --prefetch off --no-engine --no-meta
      run_case.sh --case finalize_evict --model kvc_manual_c8 --dataset kvc_manual_finalize_quick
证据: results/20261105_215909_finalize_evict/
```

- AISBench：27 请求，成功 27，失败 0；`state=finalize` POST 6 次。
- Motor：`evict_total=6`、`edit_success=6`、`edit_fail=0`、`send_fail=0`；
  `offload_total=0`、`prefetch_total=0`。
- engine：`Executing edit type=evict` 6 次，`actual_op_block_num` 合计 **985**。
- 运行中 KV usage p50 90.9%、max 100%，Waiting max 5，preemption delta 3。
- 结论：finalize → evict、session pin 释放链路正确，且不会误触发 offload/prefetch。

### S3/S4 pause → offload → resume → prefetch

```text
命令: start_stack.sh --kvc on --idle-offload 30 --high-water 0 --prefetch on --no-engine --no-meta
      run_case.sh --case pause_offload_prefetch --model kvc_manual_c8 \
                  --dataset kvc_manual_pause_offload_quick
证据: results/20261105_231030_pause_offload_prefetch/
```

- AISBench：25 请求，成功 25，失败 0；pause POST 5 次、running(resume) 5 次、finalize 4 次。
- Motor：`offload_total=5`、`prefetch_total=5`、`evict_total=4`、
  `edit_success=14`、`edit_fail=0`、`send_fail=0`、`gate_skip=0`。
- engine：offload 5 次、prefetch 5 次、evict 4 次，`actual_op_block_num` 合计 **2284**。
- KV usage p50 89.1%、max 100%，Waiting max 3，preemption delta 8。
- 结论：pause 后 idle 窗口触发 offload；resume 触发 prefetch；finalize 触发 evict。

### S5 压满触发（high_water=0.7）

```text
命令: start_stack.sh --kvc on --idle-offload 30 --high-water 0.7 --prefetch on --no-engine --no-meta
      run_case.sh --case saturated --model kvc_manual_c8 \
                  --dataset kvc_manual_saturation_pause --duration 300
证据: results/20261105_233251_saturated/
```

- 采样：KV usage p50 **95.7%**、max **100%**；Waiting max 6；preemption delta 1。
- Motor：`offload_total=8`、`prefetch_total=6`、`edit_success=14`、`edit_fail=0`。
- engine：offload 8 次、prefetch 6 次，`actual_op_block_num` 合计 **2250**。
- 结论：KV 达到高水位后，pause 能真实触发 offload/prefetch；`gate_skip=0`
  说明动作发生时 usage 始终在阈值以上。

### S6 KVC off 对照

```text
命令: start_stack.sh --kvc off --no-engine --no-meta
      run_case.sh --case kvc_off --model kvc_manual_c1 --dataset kvc_manual_gate_quick
证据: results/20261105_233822_kvc_off/
```

- AISBench：5/5 成功；pause/running/finalize POST 各 1 次，均 HTTP 200。
- Motor：`motor:kvc_*` 全部 0 增长。
- engine：无任何 edit。
- 结论：关闭 KVC 只关闭管理动作，不影响 `/agent-status` 生命周期。

### S7 high-water gate

```text
命令: start_stack.sh --kvc on --idle-offload 30 --high-water 1.0 --prefetch on --no-engine --no-meta
      run_case.sh --case gate --model kvc_manual_c1 --dataset kvc_manual_gate_quick
证据: results/20261105_234447_gate/
```

- Motor：`gate_skip_total=2`（pause 与 finalize evict 各被跳过一次），
  `offload_total=0`、`evict_total=0`、`edit_fail=0`。
- 结论：`offload_kv_usage_high_water` 门控生效；阈值未达到时不执行 engine edit。

## 4. 测试中发现的问题与修正

### 4.1 finalize 终态导致复用 session 被忽略（测试方法问题）

第一次跑完 S3 后未重启 Motor 就复用同一份数据，pause 返回：

```text
central agent_status event ... state=pause applied=False reason=terminal previous=finalize
```

原因是 `finalize` 是终态，`session_id` 复用时会拒绝后续 pause/running。
**修正**：每个 KVC-on 场景前重启 Motor，或使用新的 session_id/epoch。
已写入 README 和 case 文档。

### 4.2 数据上下文校验的两个坑（工具问题，已修复）

1. `transformers` 的 `apply_chat_template(tokenize=True)` 在当前版本返回
   `BatchEncoding`，`len()` 得到的是字段数（2），不是 token 数。
   已改为读取 `["input_ids"]`。
2. `tools=[]` 时 AISBench 会丢弃空 tools，但 `tool_choice=auto` 仍会发送，
   vLLM 返回 400：`When using tool_choice, tools must be set`。
   过滤器现在会在 tools 为空时清空 `tool_choice`。
3. 增加 256 token 安全余量，避免 `prompt + max_out` 恰好越界 1 token。

### 4.3 与性能栈的共享内存冲突（环境问题）

engine 使用 `--ipc host` 时会尝试复用 `/dev/shm/memfabric_hybrid_0`，与性能栈冲突：

```text
Local shm file is already in use /dev/shm/memfabric_hybrid_0
```

**修正**：本套件 engine 固定 `--ipc private --shm-size 64g`，拥有独立 shm 命名空间。
Motor 的 affinity/metrics shm 也使用独立 key：`MOTOR_SESSION_AFFINITY_SHM_KEY=kvcfunc`、
`metrics_shm_name=motor_kvc_metrics_kvcfunc`。

### 4.4 压满时第 3 turn 的 pause 太慢（测试数据问题）

高水位下 engine 持续 preempt，10 个 session 的第 3 个请求在 5 分钟内仍未到 pause 点。
**修正**：新增 `kvc_manual_saturation_pause.jsonl`，把 pause 移到每个 session 的第 1 turn，
使 gate/offload 在压满状态下快速触发。

### 4.5 压力下的运行时长

- `kvc_manual_pause_offload_quick`（10 session / 59 行）在高水位下 40 分钟仍未跑完；
  功能用例改用 4-5 个 session 的 `kvc_manual_pause_offload_functional`（25 行，约 16 分钟）。
- 全量 324 行数据集保留用于回归/浸泡，不作为日常手工用例。

## 5. 未覆盖项 / 已知风险

- 本轮只验证单 engine TP1；多 worker / 多 engine 的 central lifecycle 不在本轮范围。
- `prefetch` 是异步提交：`prefetch_total>0` 只代表提交成功，不代表数据已回到 HBM；
  本报告不把 TTFT 改善作为 KVC 功能判据。
- `NUM_CACHE_LEVELS=2` 下 evict 只释放本地 HBM，远端 key 为 lazy remove；
  本轮未把远端容量回收作为验收项。
- engine `op_status=true` 不等于 memcache 远端 put 成功；本轮未单独核对远端 put 失败
  （历史报告已见 `error code=-1/-2`）。
- 未执行“engine 不可达”失败语义场景（历史报告 P1：`send_fail/edit_fail` 未增加、
  `offload_total` 反而 +1），本轮不涉及该路径。

## 6. 复现入口

```bash
cd /home/t00887700/benchmark
bash tests/manual/kvc_management/scripts/build_datasets.sh
bash tests/manual/kvc_management/scripts/preflight.sh
# 按 README.md 的 S1-S7 逐项执行
```

## 7. 交付物

- `benchmark/tests/manual/kvc_management/README.md`：QA 手工执行手册。
- `benchmark/tests/manual/kvc_management/cases/*.md`：逐场景步骤与判据。
- `benchmark/tests/manual/kvc_management/scripts/*`：隔离栈、数据生成、运行、校验脚本。
- `benchmark/tests/manual/kvc_management/results/`：本轮完整证据。
- 本报告。


## 8. QA 快速入口（2026-11-06 补充）

为降低手工测试门槛，新增统一入口 `qa_run.sh`：

```bash
bash tests/manual/kvc_management/qa_run.sh doctor
bash tests/manual/kvc_management/qa_run.sh all --fast
# 打开 results/QA_REPORT_latest.html 查看 6/6 PASS/FAIL 总表
```

`--fast` 使用 `idle_offload_s=10`、`delay=15s`、3–10 个 session 的裁剪数据，
全量约 25–35 分钟；默认 standard 模式仍保留 `idle=30s`、`delay=45s` 的正式验收口径。

2026-11-06 已用 `all --fast` 完整执行一次，6/6 用例通过：

| 用例 | 结果 | 关键观测 |
|---|---|---|
| S1 | PASS | KV p50 95.0% / max 98.0%，Waiting 7 |
| S2 | PASS | 2 finalize → 2 evict / 333 blocks |
| S3 | PASS | 3 pause → 3 offload → 3 prefetch，3 evict / 1519 blocks |
| S5 | PASS | KV p50 83.5% / max 98.0%，4 offload / 2 prefetch，gate_skip=1 |
| S6 | PASS | 生命周期正常，KVC/engine edit 全 0 |
| S7 | PASS | gate_skip=2，offload=0 |

完整 fast 报告：`results/20261106_182852_qa_fast/QA_REPORT.md`（最新副本
`results/QA_REPORT_latest.md` / `.html`）。
