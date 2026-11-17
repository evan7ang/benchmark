#!/bin/bash
# 实测机快照：179.169.3.2（hostname-ef5k3.foreman.pxe）
#   8 x 910B4 32GB；单卡自然 KV 池 = 624 块（79,872 token）
#   本机上「装得下」的规模：S8（需求 528 块，比值 0.85）→ p50 210.7 -> 197.9 ms
# 用法：cp env/profiles/3.2.sh env/env.sh
# 场景根目录：从本文件向上找 config/scenario.json（profiles/ 与 env/ 两种位置都适配）
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${SCENARIO_ROOT:-}" ]; then
  :
elif [ -f "$_here/../config/scenario.json" ]; then
  SCENARIO_ROOT="$(cd "$_here/.." && pwd)"
elif [ -f "$_here/../../config/scenario.json" ]; then
  SCENARIO_ROOT="$(cd "$_here/../.." && pwd)"
else
  SCENARIO_ROOT="$(cd "$_here/../.." && pwd)"
fi

MODEL_PATH=/home/RAG_data/models/Qwen3-8B
SERVED_MODEL_NAME=kvh

ENGINE_IMAGE=agenthint-vllm:0.1.0-p3memcache
ENGINE_IMAGE_ID=sha256:de6b01676ad1f32187aac42b084e3cb02bbad1293880329558480fd8053c3c9f
MOTOR_IMAGE=mindie-motor-vllm:e2e3b
MOTOR_IMAGE_ID=sha256:d7889635b5082e29608dbf3ba4c88f43d211af8125f4a8c093a93fbc1ae36fb7

# 本机 ttft-* 即本场景的容器；共享机上请先确认归属再置 1
ALLOW_SHARED_CONTAINERS=1
ENGINE_CONTAINER=ttft-laneA-engine
MOTOR_CONTAINER=ttft-laneA-motor
NPU_INDEX=0
MASTER_PORT=29501

INFER_PORT=19475
MGMT_PORT=19476
OBS_PORT=19477
WORKER_PORT=19478
ENGINE_PORT=18220

SESSION_SHM_KEY=ttft52113laneA
KVC_METRICS_SHM_NAME=motor_kvc_metrics_ttft52113

# 32GB 卡自然池 624 块；本 profile 不压池子
POOL_BLOCKS_OVERRIDE=

ASCEND_DRIVER_DIR=/usr/local/Ascend/driver
ASCEND_INSTALL_INFO=/etc/ascend_install.info
DCMI_DIR=/usr/local/dcmi
DOCKER_RUNTIME=ascend

AH_EVIDENCE_ROOT="${AH_EVIDENCE_ROOT:-$SCENARIO_ROOT/results}"
LOG_DIR="${LOG_DIR:-$SCENARIO_ROOT/results/logs}"

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
