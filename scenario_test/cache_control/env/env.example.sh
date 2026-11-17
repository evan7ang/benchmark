#!/bin/bash
# =============================================================================
# cache_control 场景唯一环境契约
#
# 用法：
#   cp env/env.example.sh env/env.sh    # 然后按本机情况修改 env/env.sh
#   # 或直接复用一台实测机的快照：
#   cp env/profiles/3.2.sh env/env.sh
#
# 所有脚本都会 source 这个文件；除本文件外，包里不应出现任何机器相关路径。
# =============================================================================

# ---- 场景根目录（一般不用改，脚本会自己推导并覆盖）----
SCENARIO_ROOT="${SCENARIO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ---- 模型 ----
MODEL_PATH="${MODEL_PATH:-/home/RAG_data/models/Qwen3-8B}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-kvh}"

# ---- 镜像（必须按镜像 ID 校验；同名 tag 可能是被改过的镜像）----
ENGINE_IMAGE="${ENGINE_IMAGE:-agenthint-vllm:0.1.0-p3memcache}"
ENGINE_IMAGE_ID="${ENGINE_IMAGE_ID:-sha256:de6b01676ad1f32187aac42b084e3cb02bbad1293880329558480fd8053c3c9f}"
MOTOR_IMAGE="${MOTOR_IMAGE:-mindie-motor-vllm:e2e3b}"
MOTOR_IMAGE_ID="${MOTOR_IMAGE_ID:-sha256:d7889635b5082e29608dbf3ba4c88f43d211af8125f4a8c093a93fbc1ae36fb7}"

# ---- 写操作保护：容器名以 ttft- 开头时默认拒绝写操作 ----
# 新机器建议自己用 01_create_containers.sh 建独立容器（cc-*），保持默认 0；
# 只有确认 ttft-* 就是本场景容器时才设 1（共享机上很可能是别人的进程）。
ALLOW_SHARED_CONTAINERS="${ALLOW_SHARED_CONTAINERS:-0}"

# ---- 容器名 ----
ENGINE_CONTAINER="${ENGINE_CONTAINER:-ttft-laneA-engine}"
MOTOR_CONTAINER="${MOTOR_CONTAINER:-ttft-laneA-motor}"

# ---- 卡 ----
NPU_INDEX="${NPU_INDEX:-0}"                  # ASCEND_RT_VISIBLE_DEVICES
MASTER_PORT="${MASTER_PORT:-29501}"         # vllm 内部 master port，多 lane 时错开

# ---- 端口（motor 的三个端口 + 引擎端口）----
INFER_PORT="${INFER_PORT:-19475}"
MGMT_PORT="${MGMT_PORT:-19476}"
OBS_PORT="${OBS_PORT:-19477}"
WORKER_PORT="${WORKER_PORT:-19478}"
ENGINE_PORT="${ENGINE_PORT:-18220}"

# ---- 会话亲和共享内存 key（A/B 两个 lane 必须不同）----
SESSION_SHM_KEY="${SESSION_SHM_KEY:-ttft52113laneA}"
KVC_METRICS_SHM_NAME="${KVC_METRICS_SHM_NAME:-motor_kvc_metrics_ttft52113}"

# ---- 容量：池子块数 override（留空 = 用卡的自然池）----
# 128 token/块；本场景每个会话工作集 = 66 块
POOL_BLOCKS_OVERRIDE="${POOL_BLOCKS_OVERRIDE:-}"

# ---- 引擎/容器挂载（Ascend 运行时）----
ASCEND_DRIVER_DIR="${ASCEND_DRIVER_DIR:-/usr/local/Ascend/driver}"
ASCEND_INSTALL_INFO="${ASCEND_INSTALL_INFO:-/etc/ascend_install.info}"
DCMI_DIR="${DCMI_DIR:-/usr/local/dcmi}"
DOCKER_RUNTIME="${DOCKER_RUNTIME:-ascend}"  # docker run --runtime；无自定义 runtime 时置空

# ---- 证据/日志输出根（默认包内 results/，脚本会补全绝对路径）----
AH_EVIDENCE_ROOT="${AH_EVIDENCE_ROOT:-$SCENARIO_ROOT/results}"
LOG_DIR="${LOG_DIR:-$SCENARIO_ROOT/results/logs}"

# ---- 导出给 driver 的路径契约（勿随意改名）----
export AH_SCENARIO_ROOT="$SCENARIO_ROOT"
export AH_EVIDENCE_ROOT
export AH_DATA_DIR="${AH_DATA_DIR:-$SCENARIO_ROOT/data/generated}"
export AH_MODEL_NAME="$SERVED_MODEL_NAME"
export AH_A_INFER_PORT="$INFER_PORT"
export AH_A_MGMT_PORT="$MGMT_PORT"
export AH_A_OBS_PORT="$OBS_PORT"
export AH_A_ENGINE_PORT="$ENGINE_PORT"
export AH_A_MOTOR_CONTAINER="$MOTOR_CONTAINER"
export AH_A_SHM_KEY="$SESSION_SHM_KEY"

# ---- 建容器时的额外 docker run 参数（按需填写，例如 --runtime 或自定义 -e）----
ENGINE_DOCKER_EXTRA="${ENGINE_DOCKER_EXTRA:-}"
MOTOR_DOCKER_EXTRA="${MOTOR_DOCKER_EXTRA:-}"
