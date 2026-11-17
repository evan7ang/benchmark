#!/bin/bash
# 实测机快照：179.169.2.2（hostname-e1qjz.foreman.pxe）
#   2 x 910B4-1 64GB；单卡自然 KV 池 2,237 块，本场景压到 2,031 块（259,968 token）
#   本机 S32：需求 2,112 块，比值 1.04（勉强装下）→ p50 297.8-412.7 ms（off 971.6-1124.0）
# 用法：cp env/profiles/169.sh env/env.sh
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

# ★ 169 的容量旋钮：自然 2,237 块压到 2,031 块，让 32 会话「勉强装下」
POOL_BLOCKS_OVERRIDE=2031

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
