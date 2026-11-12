# Motor KVC 管理自验报告（offload / prefetch / evict）

- 测试日期：2026-11-12
- 套件：`tests/manual/kvc_management`（benchmark 仓 `feature/agent-hint-v2`）
- 结论：**fast 套件 6/6 PASS**

---

## 1. 验证概述

### 1.1 验证目标

验证 Motor 侧 KVC（KV Cache）生命周期管理的三个引擎编辑操作及配套机制：

```text
pause   -> offload     # 会话暂停：KV 块 HBM -> MemCache(DRAM)
resume  -> prefetch    # 会话恢复：KV 块 DRAM -> HBM（异步提交）
finalize -> evict      # 会话终态：释放全部 KV 块
high-water gate        # KV 使用率高水位门控（压力触发路径的开关）
KVC-off 对照           # KVC 关闭时状态通道正常但 KVC 零动作
```

### 1.2 验证范围

- 单卡 TP1（910B4 32GB），`GPU_MEM_UTIL=0.85`
- **关闭会话亲和路由**：`AFFINITY=1 + ROUTE=0`，路由走负载均衡，session pin 仅作为 KVC 定位账本
- 引擎开启 Agent Hint 与两级缓存（HBM + DRAM，`NUM_CACHE_LEVELS=2`）

### 1.3 结论摘要

| 用例 | 场景 | 结果 |
|---|---|---|
| S1 | 压满基线（KVC off） | ✅ PASS |
| S2 | finalize → evict | ✅ PASS |
| S3 | pause → offload → resume → prefetch | ✅ PASS |
| S5 | 压满触发（high_water=0.7） | ✅ PASS |
| S6 | KVC off 对照 | ✅ PASS |
| S7 | high-water gate（high_water=1.0） | ✅ PASS |

S4 与 S3 为同一场景的标准模式变体（更长 idle/delay、5 session），本报告以 fast 模式结果为准，复现方法两者完全通用。

---

## 2. 被测对象与制品构建

本章假设测试人员**只拿到源码**，需要在新 NPU 服务器上自行构建全部制品后再验证。制品共 4 件：engine 镜像、Motor 镜像、Motor wheel、AIS 镜像；模型权重另附。

### 2.1 engine 镜像 `agenthint-vllm:0.1.0-p3memcache`

**构建内容**：vLLM-Ascend 引擎 + Agent Hint 能力 + memcache_hybrid 两级缓存连接器。

**源码清单**（两个仓库的 agent-hint 分支）：

| 仓库 | 分支 | 说明 |
|---|---|---|
| `github.com/FermatGo/vllm` | `agent_hint_dev_v0.26.0_dev` | vLLM 主体，含 agent_hint 调度/编辑能力 |
| `github.com/FermatGo/vllm-ascend` | `agent_hint_dev_v0.26.0_dev_memcache` | NPU 适配层，含 memcache 连接器（`AscendStoreConnector`） |

**构建步骤**：

```bash
# 1. 准备构建工作区（build.sh 要求 vllm/ 与 vllm-ascend/ 位于 docker/ 同级）
mkdir agent-hint-build && cd agent-hint-build
git clone -b agent_hint_dev_v0.26.0_dev        https://github.com/FermatGo/vllm
git clone -b agent_hint_dev_v0.26.0_dev_memcache https://github.com/FermatGo/vllm-ascend
cp -r <docker目录，含 Dockerfile / build.sh / agenthint-entrypoint.sh> ./docker

# 2. 构建（基础镜像为 Ascend 官方 vllm-ascend，digest 已固定在 build.sh；
#         构建只替换 Python 包树，保留 CANN / torch_npu / 预编译扩展）
TAG=agenthint-vllm:0.1.0-p3memcache AGENTHINT_VERSION=0.1.0 bash docker/build.sh
```

**产物验证**：

```bash
docker image inspect agenthint-vllm:0.1.0-p3memcache --format '{{json .Config.Labels}}'
# labels 应含 agenthint.vllm.sha / agenthint.vllm_ascend.sha（构建时自动钉入）

docker run --rm --entrypoint bash agenthint-vllm:0.1.0-p3memcache -lc \
  'python3 -c "from memcache_hybrid import MetaService; print(\"memcache OK\")"'
```

> 新服务器若无法访问 `quay.nju.edu.cn`，可将基础镜像 `quay.nju.edu.cn/ascend/vllm-ascend@sha256:38fcc9b6...`（同 `quay.io/ascend/vllm-ascend`）用 `docker save/load` 离线搬运，或设置 `BASE_IMAGE=<本地基础镜像>` 覆盖。

### 2.2 Motor 镜像 `mindie-motor-vllm:e2e3b`（运行底座）

**定位**：Motor 的运行底座（python3.12 + 运行依赖）。本测试中镜像内自带的 motor 包**不参与被测**——实际被测代码由 2.3 节的 wheel 覆盖安装。

**两种获取方式（任选其一）**：

```bash
# 方式 A：从 AscendHub 拉取官方 mindie-motor 镜像（推荐，省去构建）
# 方式 B：用 MindIE-Motor 仓内置的自包含 Dockerfile 构建：
git clone -b v3.1.0 https://gitcode.com/Ascend/MindIE-Motor.git
cd MindIE-Motor/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a3-ubuntu22.04-py3.12
docker build --network=host --platform=linux/arm64 \
  -t mindie-motor-vllm:e2e3b -f Dockerfile .
```

要求：底座镜像的 python 版本必须为 **3.12**（wheel 的 ABI 与依赖匹配），且为 aarch64。

### 2.3 Motor wheel（被测对象本体，从源码构建）

**被测代码**：`evanTang/MindIE-Motor` @ `feature/agent-hint-kvc-lifecycle`（本次验证 commit `fccec0f6`）。

**构建步骤**（在 2.2 的底座镜像容器内进行，保证 ABI/依赖一致）：

```bash
git clone -b feature/agent-hint-kvc-lifecycle https://gitcode.com/evanTang/MindIE-Motor.git
cd MindIE-Motor

docker run --rm --network host --entrypoint bash \
  -v "$PWD:/src" -w /src -e SKIP_RUST_BUILD=1 \
  mindie-motor-vllm:e2e3b -lc 'bash build.sh'
# 产出：dist/motor-3.1.0-py3-none-linux_aarch64.whl
# 说明：SKIP_RUST_BUILD=1 复用源码树内已有的 workload-shm 动态库
#       （motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so）；
#       若源码树缺失该 .so，则去掉 SKIP 并准备 Rust 工具链。
```

**产物验证**（wheel 必须包含 KVC 生命周期实现）：

```bash
unzip -q -o dist/motor-3.1.0-py3-none-linux_aarch64.whl -d /tmp/whl
ls /tmp/whl/motor/coordinator/scheduler/runtime/agent_hint_lifecycle.py   # 必须存在
grep -c "kvc_lifecycle" /tmp/whl/motor/config/coordinator.py             # 必须 > 0
grep -n "_apply_kvc_lifecycle" /tmp/whl/motor/coordinator/api_server/inference_server.py  # 必须有接线
```

### 2.4 AIS 镜像 `ais-runner:ais-1`（压测客户端）

**构建内容**：AISBench 运行环境（`ais_bench` CLI + 全部 python 依赖）。

**关键机制**：镜像**只装依赖和 CLI 入口**；运行时用例代码由 benchmark 源码目录实时挂载注入（`/workspace`）。因此 AISBench 侧的迭代不需要重建本镜像。

**Dockerfile**（已入库：`docker/Dockerfile.ais-runner`，完整内容如下）：

```dockerfile
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
    PIP_TRUSTED_HOST=repo.huaweicloud.com PIP_TIMEOUT=120 PIP_RETRIES=10
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev curl ca-certificates unzip build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY benchmark /opt/benchmark
WORKDIR /opt/benchmark
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    python3 -m pip install --no-cache-dir -e . && \
    python3 -m pip install --no-cache-dir "transformers>=4.51,<5"
RUN ais_bench --help >/dev/null 2>&1 && echo "ais_bench CLI OK" && \
    python3 -c "import transformers; print('transformers', transformers.__version__)"
CMD ["bash"]
```

```bash
# 构建（构建上下文需包含 benchmark 源码目录，排除 .git 与 work/）
docker build -t ais-runner:ais-1 .
```

要求：benchmark 源码为 `evan7ang/benchmark` @ `feature/agent-hint-v2` 且 **commit ≥ `c199768`**（含四态 wire 协议适配）。`transformers` 必须 <5（runtime.txt 未锁版本会装到 5.x，存在 tokenizer 兼容风险）。

### 2.5 版本可追溯性

环境就绪后运行 `./qa_run.sh doctor`，会自动生成 `work/MANIFEST.json` 固化当次验证的全部版本指纹（benchmark commit、三个镜像 ID、wheel 安装模式、模型路径/名/上下文长度）。执行用例前保留此文件即完成版本留档。

---

## 3. 测试环境与解耦契约

### 3.1 服务器前置条件

- Linux + Docker，支持 `--network host`、`--privileged`、Ascend 设备直通
- ≥1 张空闲 910B4（32GB HBM），驱动与 engine 镜像匹配（doctor 会检查）
- 宿主机 `python3 ≥ 3.9`、`curl`、`bash`、`tar`
- 模型权重目录已就位（本验证用 Qwen3-8B bf16：16.4GB / 36 层 / `Qwen3ForCausalLM` / max_pos 40960）

### 3.2 交付物清单

| 交付物 | 形态 | 新服务器获取方式 |
|---|---|---|
| benchmark 测试套件 | 源码 | `git clone -b feature/agent-hint-v2 https://github.com/evan7ang/benchmark` |
| engine 镜像 | 镜像或源码 | 按 §2.1 构建（或 `docker load -i engine.tar`） |
| Motor 底座镜像 | 镜像 | 按 §2.2 拉取/构建（或 `docker load -i motor.tar`） |
| Motor wheel | 构建产物或源码 | 按 §2.3 构建（或拷贝 wheel 文件） |
| AIS 镜像 | 镜像或源码 | 按 §2.4 构建（或 `docker load -i ais-runner.tar`） |
| 模型目录 | 文件 | 拷贝 Qwen3-8B 完整目录（config/tokenizer/5 个权重分片齐全） |

### 3.3 `kvc_test.env` 全字段释义

套件唯一配置入口，加载优先级：**显式环境变量 > `KVC_TEST_ENV` 指定文件 > `./kvc_test.env` > 内置默认值**。所有脚本通过 `scripts/common.sh` 读取。

| 字段 | 本次取值 | 含义 / 消费方 / 注意点 |
|---|---|---|
| `BENCH_SOURCE_DIR` | `/home/.../benchmark` | AISBench v2 源码目录，运行时挂载进 AIS/Motor 容器。**改代码免重建 AIS 镜像**的前提 |
| `MODEL_PATH` | `/mnt/weight/Qwen3-8B` | 模型目录。engine `--model`；同时挂载进三个容器；dataset 生成时做 tokenizer 上下文校验（doctor 检查路径与 config.json） |
| `TOKENIZER_PATH` | 空=MODEL_PATH | 数据集校验的 tokenizer 路径，一般不用单独配 |
| `MODEL_NAME` | `kvh` | engine `--served-model-name`；**必须与 AISBench 请求体 `model` 字段一致** |
| `MAX_MODEL_LEN` | 32768 | engine `--max-model-len`；数据集生成上限基准（校验式：prompt+max_out+256 ≤ 本值） |
| `CHAT_API` | true | true 走 `/v1/chat/completions`（messages 体）；false 走 `/v1/completions` |
| `TP_SIZE` | 1 | 张量并行度 |
| `CARD` / `CARDS` | 1 / 空 | 单卡号；`CARDS` 非空时优先生效（如 `0,1` 做 TP2）。**选卡标准：doctor 的 NPU 空闲显存检查需通过**（占用 <10% 为 OK） |
| `GPU_MEM_UTIL` | 0.85 | **语义是总显存的目标占用比例（含其他进程）**：vLLM 按 `0.85×32GB − 已占用` 分配自身预算。卡上有他人进程时实际 KV 池会缩小，S5 压满水位会相应变化，不影响功能判据 |
| `ENGINE_START_TIMEOUT` | 300 | engine 就绪等待秒数（16GB 权重实测加载+预热约 4.5 分钟） |
| `ENGINE_IMAGE` / `MOTOR_IMAGE` / `AIS_IMAGE` | 见 §2 | 三个镜像 tag，必须与构建/导入的 tag 一致 |
| `MOTOR_INSTALL_MODE` | `wheel` | `image`=用底座镜像自带 motor；`wheel`=安装 `MOTOR_WHEEL` 覆盖；`source`=取 `MOTOR_SOURCE_DIR/dist` 最新 wheel。**被测 motor 为 wheel 模式** |
| `MOTOR_WHEEL` | `dist/motor-3.1.0-...whl` | wheel 绝对路径（容器内 pip `--force-reinstall --no-deps` 安装，依赖沿用底座镜像） |
| `MOTOR_SOURCE_DIR` | 空 | source 模式的 motor 源码目录 |
| `WORK_DIR` / `DATA_DIR` / `RESULTS_DIR` / `GENERATED_CONFIG_DIR` | kit 内 `work/` 及子目录 | 运行/数据/结果/渲染配置目录。**容器与主机同路径挂载**，需可写；doctor 自动创建 |
| `ENGINE_PORT` | 18000 | engine 服务端口 |
| `MOTOR_INFER/MGMT/OBS_PORT` | 19125/19126/19127 | Motor 三端口（infer=业务入口，接收 /agent-status 与 chat；mgmt=管理；obs=指标） |
| `MMC_META/STORE/METRICS/LOOKUP_PORT` | 15000/16000/18181/19100 | MemCache 四端口 |
| `HOST_IP` | 127.0.0.1 | 全部回环访问；healthcheck/采样/AISBench 请求共用 |
| `DOCKER_BIN` | docker | docker 命令名 |
| `KVC_CONTAINER_PREFIX` | kvcfunc | 容器名前缀（`kvcfunc-motor/-engine-0/-memcache-meta`）。**同机并行多套时必改**，shm key 随之区分 |
| `AIS_RUN_MODE` | docker | docker=容器跑 AISBench；host=宿主机直跑（需宿主机装 ais_bench） |
| `AIS_PRE_CMD` | `pip uninstall -y av ...` | AISBench 容器启动前预处理（卸载 av 防段错误，README 坑 #7），已固化默认 |
| `ENGINE_TOOL_ARGS` | `--enable-auto-tool-choice --tool-call-parser hermes` | engine 启动参数（Qwen 系工具调用） |
| `ENGINE_EXTRA_ARGS` | 空 | engine 额外参数注入口 |
| `MEMCACHE_LIB_DIR` / `PYTHON_LIB_DIR` | 容器内路径 | memcache_hybrid 动态库位置，engine 启动拼入 `LD_LIBRARY_PATH`。**必须与 engine 镜像内实际路径一致**（本镜像 python3.12.13） |
| `KVC_DATA_MODE` | synthetic | `synthetic`=tokenizer 现场生成确定性长 prompt（推荐）；`legacy`=SWE-bench 衍生数据（需 `SOURCE_DATA_DIR`）；`external`=DATA_DIR 预置数据 |
| `SOURCE_DATA_DIR` | 空 | legacy 模式数据源 |
| `FAST_IDLE_OFFLOAD` / `STD_IDLE_OFFLOAD` | 10 / 30 | Motor idle offload 定时器（fast/standard）。**必须 < 对应模式的 DELAY_MS**，否则 resume 先于 offload 到达 |
| `FAST_DELAY_MS` / `STD_DELAY_MS` | 15000 / 45000 | pause 行发完后客户端睡眠毫秒数（给定时器留触发窗口） |
| `FAST_S1_DURATION` / `FAST_S5_DURATION` | 120 / 180 | S1/S5 压测掐断秒数 |
| `STD_S1_DURATION` / `STD_S5_DURATION` | 180 / 300 | standard 模式对应值 |
| `ASCEND_DRIVER_PATH` / `ASCEND_INSTALL_INFO` / `ASCEND_DCMI` / `ASCEND_SET_ENV` | 默认 | NPU 驱动/安装信息/dcmi/环境脚本路径，doctor 检查存在性 |
| `MMC_WORLD_SIZE` / `MMC_DRAM_SIZE` / `MMC_MAX_DRAM_SIZE` / `NUM_CACHE_LEVELS` | 1 / 64GB / 256GB / 2 | MemCache 拓扑：DRAM 缓存容量（offload 目的地）与缓存级数 |
| `KVC_SHM_KEY` / `KVC_METRICS_SHM_NAME` | kvcfunc / motor_kvc_metrics_kvcfunc | Motor 亲和表与 KVC 指标的共享内存名，**多实例隔离关键** |

### 3.4 隔离约定（固定值，勿改）

```bash
MOTOR_SESSION_AFFINITY=1            # 必须开：KVC 依赖 session location pin
MOTOR_SESSION_AFFINITY_SHARED=1
MOTOR_SESSION_AFFINITY_PLACEMENT=balanced
MOTOR_SESSION_AFFINITY_ROUTE=0      # 路由纯负载均衡，pin 只做 KVC 账本
```

engine 容器固定 `--ipc private`（避免与性能栈的 `/dev/shm/memfabric_hybrid_*` 冲突）。

### 3.5 换新服务器最小改动清单

仅需 4 项 + 环境检查：

1. `MODEL_PATH` / `TOKENIZER_PATH` → 目标模型目录
2. `MODEL_NAME` → 服务名（同步 AISBench 配置模板）
3. 三个镜像 tag → 与本地构建/导入一致
4. `CARD` → doctor 显示空闲的那张卡

然后 `./qa_run.sh doctor` 全绿即可开跑。端口默认 8 个均空闲；若被占用，整组改 `ENGINE_PORT`/`MOTOR_*_PORT`/`MMC_*_PORT`。

---

## 4. 端到端手动复现手册

### 4.1 流程总览

```text
qa_run.sh init            # 生成 kvc_test.env
编辑 kvc_test.env          # 3.5 节的 4 项
qa_run.sh doctor          # 环境体检，必须 0 failures
qa_run.sh datasets --fast # 生成 + tokenizer 校验数据集

# S1（首次全栈启动，engine 加载约 4.5 分钟）
start_stack.sh --kvc off ...      # 全栈：memcache + engine + Motor
run_case.sh --case ramp_kvc_off_pause_c8 ... --duration 120

# S2–S7（engine/MemCache 复用，每个用例前仅重启 Motor）
start_stack.sh --kvc <on|off> --idle-offload N --high-water H --prefetch P --no-engine --no-meta
run_case.sh --case <case> ...

stop_stack.sh             # 全部跑完后停栈
```

> 便捷方式：每个用例也可用 `./qa_run.sh case s<N> --fast` 一键完成"重启 Motor + 跑用例"（`KEEP_STACK=1` 保留栈便于连跑）。本章给出的是底层手工命令，便于理解每一步在做什么。

### 4.2 环境准备

```bash
cd tests/manual/kvc_management
./qa_run.sh init
vi kvc_test.env          # 按 3.5 改 4 项；MOTOR_INSTALL_MODE=wheel + MOTOR_WHEEL 按 2.3
./qa_run.sh doctor
```

**通过标准**：末尾输出 `===== doctor summary: failures=0 warnings=0 =====`。重点看：三个镜像 OK、模型文件 OK、`AISBench image + source has delay_after_ms support` OK、8 端口空闲、目标卡 NPU 显存占用 <10%。

### 4.3 数据集生成与校验

```bash
./qa_run.sh datasets --fast
```

**预期**：输出 12 套数据集（fast/standard 各 6），每套带 `*.report.json`（行数/session 数/状态分布/prompt token 统计）；同时渲染 AISBench 配置到 `work/generated_configs/`。

**验证**：

```bash
ls work/data/*.jsonl | wc -l                       # ≥ 12
python3 -m json.tool work/data/kvc_manual_pause_offload_functional_fast.jsonl.report.json | head
# prompt_tokens.max + max_out_len + 256 ≤ MAX_MODEL_LEN，即无超限
```

### 4.4 栈启动的两种模式

**首次（S1）— 全栈启动**：

```bash
bash scripts/start_stack.sh --kvc off --card 1 --port 18000 \
  --idle-offload 0 --high-water 0 --prefetch off
# 依次起 memcache meta -> engine（约 4.5 分钟）-> Motor
# "isolated KVC functional stack is ready" 即就绪
```

**后续用例 — 仅重启 Motor**（engine/MemCache 复用；**每个 KVC-on 场景之间必须重启**，因为 finalize 是终态，session_id 会复用，不重启会让 pause 返回 `applied=false reason=terminal`）：

```bash
bash scripts/start_stack.sh --kvc <on|off> --idle-offload <N> --high-water <H> --prefetch <on|off> \
  --no-engine --no-meta
```

### 4.5 逐用例执行（三段式）

#### S1 压满基线（KVC off）

**① 命令**（首次全栈）：

```bash
bash scripts/start_stack.sh --kvc off --idle-offload 0 --high-water 0 --prefetch off
bash scripts/run_case.sh --case ramp_kvc_off_pause_c8 \
  --model kvc_manual_c8 --dataset kvc_manual_pause_offload_multi --duration 120
```

**② 预期**：AISBench 120s 后被掐断（exit=137）；`engine metrics: kv_usage_max≈75%`、`waiting_max>0`；KVC 计数全 0、engine 无 edit。

**③ 验证**：

```bash
cat work/results/<ts>_ramp_kvc_off_pause_c8/summary.txt   # PASS: True
grep -E 'kv_cache_usage_perc|num_requests_waiting' work/results/<ts>_ramp_kvc_off_pause_c8/engine-metrics.csv | tail -5
```

#### S2 finalize → evict

**① 命令**：

```bash
bash scripts/start_stack.sh --kvc on --idle-offload 0 --high-water 0 --prefetch off --no-engine --no-meta
bash scripts/run_case.sh --case finalize_evict --model kvc_manual_c8 --dataset kvc_manual_finalize_fast
```

**② 预期**：4 个 finalize POST → `evict_total=4`（与 finalize 数一致）、`edit_success_total=4`；`offload_total=prefetch_total=0`；engine 日志 `Executing edit type=evict` 且 block 数 >0。

**③ 验证**：

```bash
cat work/results/<ts>_finalize_evict/summary.txt
grep -E 'kvc evict done|central agent_status' work/results/<ts>_finalize_evict/coordinator-kvc.log | head
grep -E 'Executing edit|actual_op_block_num' work/results/<ts>_finalize_evict/engine-kvc.log | head
```

#### S3 pause → offload → resume → prefetch

**① 命令**：

```bash
bash scripts/start_stack.sh --kvc on --idle-offload 10 --high-water 0 --prefetch on --no-engine --no-meta
bash scripts/run_case.sh --case pause_offload_prefetch --model kvc_manual_c8 --dataset kvc_manual_pause_offload_fast
```

**② 预期**：`offload_total=3`（=pause 数）；`prefetch_total=3` 且 ≤ offload；`edit_fail=send_fail=0`；coordinator 日志时序为 `pause(+10s)→offload done→resume→prefetch done→finalize→evict done`。

**③ 验证**：

```bash
cat work/results/<ts>_pause_offload_prefetch/summary.txt
grep -E 'central agent_status|kvc (offload|prefetch|evict) done' work/logs/coordinator-kvcfunc.log | tail -20
# 对照时间戳：offload 应发生在 pause 后 ~10s（FAST_IDLE_OFFLOAD），prefetch 紧跟 resume
```

#### S5 压满触发（high_water=0.7）

**① 命令**：

```bash
bash scripts/start_stack.sh --kvc on --idle-offload 10 --high-water 0.7 --prefetch on --no-engine --no-meta
bash scripts/run_case.sh --case saturated --model kvc_manual_c8 \
  --dataset kvc_manual_saturation_pause_fast --duration 180
```

**② 预期**：`kv_usage_max ≥ 70%`（历史 98%+）或 `waiting>0`；`offload_total>0`；`gate_skip` 可 >0（offload 后水位回落被门跳过的自限现象，属正常）；`edit_fail=0`。

**③ 验证**：

```bash
cat work/results/<ts>_saturated/summary.txt
python3 -c "
import csv
rows = list(csv.DictReader(open('work/results/<ts>_saturated/engine-metrics.csv')))
kv = [float(r['engine_kv_cache_usage_perc']) for r in rows if r.get('engine_kv_cache_usage_perc')]
print('kv max %.1f%%  p50 %.1f%%' % (max(kv), sorted(kv)[len(kv)//2]))"
```

#### S6 KVC off 对照

**① 命令**：

```bash
bash scripts/start_stack.sh --kvc off --idle-offload 0 --high-water 0 --prefetch off --no-engine --no-meta
bash scripts/run_case.sh --case kvc_off --model kvc_manual_c1 --dataset kvc_manual_gate_fast
```

**② 预期**：agent-status POST（pause/resume/finalize）全部 HTTP 200、`applied=True`；Motor `kvc_*` 全部不增长；engine 无 edit；请求成功率 100%。

**③ 验证**：

```bash
cat work/results/<ts>_kvc_off/summary.txt
grep 'central agent_status' work/logs/coordinator-kvcfunc.log | tail -5   # applied=True
```

#### S7 high-water gate（high_water=1.0）

**① 命令**：

```bash
bash scripts/start_stack.sh --kvc on --idle-offload 10 --high-water 1.0 --prefetch on --no-engine --no-meta
bash scripts/run_case.sh --case gate --model kvc_manual_c1 --dataset kvc_manual_gate_fast
```

**② 预期**：pause 触发的 offload 检查因水位（~6%）远不到 1.0 全部被门跳过：`gate_skip_total=2`、`offload_total=0`、`evict_total=0`、`edit_fail=0`。

**③ 验证**：

```bash
cat work/results/<ts>_gate/summary.txt
```

#### 收尾

```bash
bash scripts/stop_stack.sh        # 停 memcache + engine + Motor
```

### 4.6 结果产物结构

每个用例在 `work/results/<时间戳>_<case>/` 下产出：

```text
summary.txt / summary.json  # 自动判据结论（PASS + 指标 + 失败原因）
aisbench/                   # AISBench 原始输出（details 含每行成功率）
engine-metrics.csv          # 1s 采样：KV usage / waiting / preemption
motor-before/after.prom     # Motor KVC 指标前后快照
coordinator-kvc.log         # 过滤后的 Motor KVC 日志（dispatch/done/状态事件）
engine-kvc.log              # 过滤后的 engine edit 日志
aisbench.log / run_rc.txt   # 容器 stdout / 退出码（137=到时掐断，正常）
```

---

## 5. 用例设计原理

### 5.1 场景矩阵

| 用例 | 栈参数（kvc/idle/high/prefetch） | 模型并发 | 数据集 | 触发路径 | 判据核心 |
|---|---|---|---|---|---|
| S1 | off/0/0/off | c8 | pause_offload_multi | — | KV≥70% 或 Waiting>0 |
| S2 | on/0/0/off | c8 | finalize_fast(4s) | finalize 立即 evict | evict=finalize 数；offload/prefetch=0 |
| S3 | on/10/0/on | c8 | pause_offload_fast(3s) | **idle 定时器** | offload=pause 数；0<prefetch≤offload |
| S5 | on/10/0.7/on | c8 | saturation_pause_fast(10s) | **高水位门控** | KV≥70%；offload>0；gate_skip 可>0 |
| S6 | off/0/0/off | c1 | gate_fast(1s) | — | POST 200；KVC/edit 全 0 |
| S7 | on/10/1.0/on | c1 | gate_fast(1s) | 门永远关闭 | gate_skip>0；offload=evict=0 |

（s=N 表示数据集含 N 个 session。）

### 5.2 数据集设计

`synthetic` 模式用模型 tokenizer 现场生成**确定性长 prompt**（可复现），每行结构：

```json
{
  "question": "<行标识>",
  "max_out_len": 256,
  "agent_hint": {"session": {"session_id": "bench_synthetic_..._NN"},
                  "agent_status": {"state": "running|pause|resume|finalize"}},
  "delay_after_ms": null | 15000,          // 仅 pause 行有值
  "messages": [{"role": "user", "content": "<8k~29k token 确定性文本>"}],
  "tools": []
}
```

关键时序约束：**`DELAY_MS(15s) > IDLE_OFFLOAD(10s)`**。pause 行发完后客户端睡眠 15s，Motor 的 10s 定时器在睡眠期内到期完成 offload，resume 到达时数据已在 DRAM，prefetch 才有意义。

### 5.3 发送机制与并发模型

每个数据行产生两个 HTTP 调用（发往 Motor infer 端口）：

1. `POST /agent-status`：`{"session":{"session_id":...}, "agent_status":{"state":...}}` —— 生命周期事件，Motor 状态机的输入
2. `POST /v1/chat/completions`：`agent_hint` 走 HTTP header，body 为 messages + `temperature=0` + `ignore_eos=true` —— 实际推理负载

**四态 wire 协议**：`running / pause / resume / finalize`（`tool`/`suspend` 为旧别名，Motor 归一到 pause；`start`→running、`end`→finalize 由客户端折叠）。其中 **resume 是独立状态**：Motor 只认显式 resume 为 prefetch 触发器——pause 后若只收到 running，Motor 会告警 `running while offloaded without explicit resume` 并等待 resume，这是四态收敛后的设计语义。

**并发模型**（`session_aware` + `session_concurrency`）：dispatcher 把行按 session_id 路由到 per-session 队列；**session 内严格串行**（最多 1 个 in-flight，保证生命周期因果），session 之间并发（c8=同时最多 8 个 session）。pause/finalize 行发完请求后会"分裂"worker 并释放并发槽位，配合 delay 睡眠实现空闲窗口。

**上报时机**：resume 行在**请求前**上报（恢复信号先行）；pause/finalize 行在**请求完成后**上报。

### 5.4 KVC 触发原理

| 路径 | 配置 | 语义 |
|---|---|---|
| idle 定时器 | `idle_offload_s>0` 且状态=pause | pause 事件 arm 定时器，到期检查（状态仍为 pause、未 offloaded/finalized）→ 过压力门 → offload |
| finalize | 状态=finalize | 立即走同一队列下发 evict（终态不可逆，重复 finalize 幂等忽略） |
| 压力门控 | `offload_kv_usage_high_water` | `<=0`：门关闭恒放行（功能验证）；`>0`：仅当某 engine 的 KV usage ≥ 阈值才放行（压力触发）。门跳过计数 `gate_skip_total` |
| prefetch | 显式 resume 且 `prefetch_enabled=on` | 检查 session 确在 offloaded 状态 → 异步提交（`prefetch_total>0` 只代表已提交，不代表已回 HBM） |

三个操作统一经 Motor 内部串行队列下发到持有该 session pin 的 engine（`send_session_manage_request` → engine 执行 `Executing edit type=<offload|prefetch|evict>`）。

---

## 6. 测试结果

### 6.1 汇总（2026-11-12，fast 模式，卡 1）

| 用例 | 关键指标 | 判据 | 结果 |
|---|---|---|---|
| S1 | KV max 74.6% / p50 71.5%，Waiting max 7 | KV≥70% 或 W>0 | ✅ |
| S2 | finalize POST=4 → evict=4 / **360 blocks**；offload=prefetch=0；edit 4/0/0 | evict=finalize 数；fail=0 | ✅ |
| S3 | offload=3 → prefetch=3 → evict=3；edit 9/0/0；**786 blocks** | offload=pause 数；0<pf≤of；fail=0 | ✅ |
| S5 | KV max **98.5%** / p50 80.6%；offload=10 / prefetch=6；**gate_skip=6**；Waiting 7 | KV≥70%；offload>0；fail=0 | ✅ |
| S6 | POST 全部 200/applied；`kvc_*` 全 0；engine edit 0；成功率 100% | 通道通 + 零动作 | ✅ |
| S7 | gate_skip=2；offload=0；evict=0；edit_fail=0 | 门拦截 | ✅ |

### 6.2 关键时序证据

**S3 单 session 全链路**（coordinator 日志，ObsServer 进程）：

```text
02:36:32  agent_status state=pause   applied=True        # 3 个 session
02:36:42  kvc manage dispatch edit=offload → done        # pause 后整 10s（idle_offload_s=10）
02:36:47  agent_status state=resume  previous=pause      # 客户端 15s 睡眠结束
02:36:47  kvc manage dispatch edit=prefetch → done        # resume 触发，异步提交
02:37:29  agent_status state=finalize
02:37:29  kvc manage dispatch edit=evict → done
```

**S5 门控自限**（`gate_skip=6`）：每次 offload 释放 HBM 后水位回落跌破 70%，后续 pause 定时器到期的 offload 尝试被门跳过——`gate_skip_total` 增长而 `offload_total` 停在 10，与 README"自限是正常现象"的预期一致。

### 6.3 证据目录

```text
work/results/20261112_011535_S1/   # 压满基线
work/results/20261112_015506_S2/   # finalize→evict
work/results/20261112_023511_S3/   # pause→offload→resume→prefetch（最终 PASS 轮）
work/results/20261112_061759_S6/   # KVC off 对照
work/results/20261112_061930_S7/   # high-water gate
work/results/20261112_062121_S5/   # 压满触发
work/MANIFEST.json                 # 版本指纹（doctor 生成）
```

---

## 7. 结果分析

### 7.1 offload（两个触发路径均验证通过）

- **定时器路径（S3）**：pause → 整 10s → offload done，3/3 全部命中，与配置的理论时序精确一致；block 级 HBM→DRAM 搬运由 engine 确认执行。
- **压力路径（S5）**：10 个 pause 全部在高水位（≥70%）下放行，offload 成为压满状态下的泄压手段；`gate_skip=6` 证明门控"按水位放行/拦截"的语义真实生效而非摆设。

### 7.2 prefetch（resume 显式触发）

S3 中 3 个显式 resume 全部触发 prefetch 且 `prefetch ≤ offload`（同 session 数）；异步提交语义符合设计（提交成功即计数，不阻塞后续请求）。S5 中 6/10 的 prefetch 比例与历史运行（6~8/10）相当——resume 晚于掐断时间或未 offloaded 的 session 不产生 prefetch，符合预期。

### 7.3 evict（finalize 终态一致性）

S2 中 evict 数与 finalize POST 数**严格一致**（4=4），block 释放量 360；S3 中 finalize→evict 3/3。终态语义正确：evict 后 session 的 HBM/DRAM 块全部释放，重复 finalize 幂等。

### 7.4 门控与对照

- **S7**：`high_water=1.0` 下 2 次 offload 尝试全部门跳过、零 edit——门在"永不达标"配置下正确拦截。
- **S6**：KVC off 时 pause/resume/finalize POST 全部 200 applied，而 KVC 计数与 engine edit 严格为零——证明状态通道与 KVC 执行器是独立开关，为所有 KVC-on 用例提供了归因基线（排除"客户端没发出来"的假象）。
- **S1**：c8 压满基线成立（KV max 74.6%、Waiting 7），为 S5 的"KVC 介入前水位"提供参照。

### 7.5 水位观察

S5 的 KV 曲线呈"压满→offload 泄压→再压满"的锯齿形态（p50 80.6% / max 98.5%），与 S1 无 KVC 时的单边爬坡形成对照，直观呈现 KVC 管理对显存碎片的回收效果。

---

## 8. 附录

### A. 关键日志检索速查

```bash
# Motor 侧：KVC 三操作 dispatch/完成 + 生命周期事件（全量日志在 work/logs/coordinator-kvcfunc.log）
grep -E 'kvc (offload|prefetch|evict) done|kvc manage dispatch|central agent_status' work/logs/coordinator-kvcfunc.log

# 用例目录内的过滤版
grep -E 'kvc (offload|prefetch|evict)|gate_skip' work/results/<ts>_<case>/coordinator-kvc.log

# engine 侧：edit 执行与 block 数
grep -E 'Executing edit|actual_op_block_num' work/results/<ts>_<case>/engine-kvc.log

# 实时观察（跑用例时另开终端）
./qa_run.sh observe     # KV usage / Waiting / KVC 计数 实时面板
```

### B. 复现常见问题 FAQ

1. **pause 返回 `applied=false reason=terminal`**：上一轮 finalize 过同 session_id。每个 KVC-on 场景前重启 Motor（`--no-engine --no-meta`）。
2. **`kvc offload skipped: no session pin`**：会话亲和被完全关闭。保持 `AFFINITY=1 + ROUTE=0` 的固定组合，不要设 `MOTOR_SESSION_AFFINITY=0`。
3. **`gate_skip` 涨但 `offload_total` 不涨**：水位未到 `high_water`（S5 属正常自限）；功能验证场景请用 `--high-water 0`。
4. **engine 启动报 `/dev/shm/memfabric_hybrid_* already in use`**：engine 被改成 `--ipc host` 了，套件固定 `--ipc private`。
5. **请求 400 `maximum context length`**：手工改过数据未做 tokenizer 校验，重跑 `./qa_run.sh datasets --fast`。
6. **请求 400 `When using tool_choice, tools must be set`**：`tools=[]` 残留 `tool_choice`，上下文过滤器会自动清理，勿绕过。
7. **AISBench 容器段错误**：`AIS_PRE_CMD` 已固化 `pip uninstall -y av`，确认 env 中未被覆盖。
8. **`--no-engine` 启动后 Motor 无可用 engine**：`start_stack.sh` 会检查 `ENGINE_PORT/v1/models`，先起 engine 或去掉 `--no-engine`。
9. **doctor 报 NPU 占用高**：换 `CARD` 到空闲卡（占用 <10% 为 OK）。

### C. 本报告引用的版本指纹

| 制品 | 标识 |
|---|---|
| benchmark 套件 | `evan7ang/benchmark` @ `feature/agent-hint-v2`，commit `c199768` |
| Motor 被测代码 | `evanTang/MindIE-Motor` @ `feature/agent-hint-kvc-lifecycle`，commit `fccec0f6`，wheel `motor-3.1.0-py3-none-linux_aarch64.whl` |
| engine 镜像 | `agenthint-vllm:0.1.0-p3memcache`（labels 含 vllm/vllm-ascend SHA） |
| Motor 底座镜像 | `mindie-motor-vllm:e2e3b`（py3.12 / aarch64） |
| AIS 镜像 | `ais-runner:ais-1`（benchmark 源码挂载注入） |
| 模型 | Qwen3-8B bf16（16.4GB，36 层，`Qwen3ForCausalLM`，max_pos 40960） |
| 运行环境 | 8×910B4（32GB），`CARD=1`，`GPU_MEM_UTIL=0.85` |
