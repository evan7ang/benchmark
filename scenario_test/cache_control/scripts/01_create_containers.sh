#!/bin/bash
# 01 创建引擎/网关容器（幂等：已存在则跳过）。
# 参数全部来自 env/env.sh；参考机规格：
#   引擎: privileged, ipc=private, shm=64g, network=host, --runtime=ascend, /dev/davinci<N>
#   网关: network=host, ipc=host, shm=64m（纯 HTTP，不挂卡）
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

guard_container "$ENGINE_CONTAINER"
guard_container "$MOTOR_CONTAINER"

RUN_ARGS=()
[ -n "${DOCKER_RUNTIME:-}" ] && RUN_ARGS+=(--runtime "$DOCKER_RUNTIME")

if container_exists "$ENGINE_CONTAINER"; then
  say "跳过已存在的引擎容器: $ENGINE_CONTAINER"
else
  say "创建引擎容器: $ENGINE_CONTAINER (image=$ENGINE_IMAGE, NPU=$NPU_INDEX)"
  docker run -d --name "$ENGINE_CONTAINER" \
    --privileged --ipc=private --shm-size=64g --network=host \
    "${RUN_ARGS[@]}" \
    --device="/dev/davinci${NPU_INDEX}" \
    --device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc \
    -v "${ASCEND_DRIVER_DIR}:${ASCEND_DRIVER_DIR}" \
    -v "${ASCEND_INSTALL_INFO}:${ASCEND_INSTALL_INFO}" \
    -v "${DCMI_DIR}:${DCMI_DIR}" \
    -v "${MODEL_PATH}:${MODEL_PATH}" \
    -v "${SCENARIO_ROOT}:${SCENARIO_ROOT}" \
    -e VLLM_ASCEND_ENABLE_AGENT_HINT=1 \
    -e ASCEND_RT_VISIBLE_DEVICES="$NPU_INDEX" \
    -e TASK_QUEUE_ENABLE=1 \
    -e VLLM_LOGGING_LEVEL=INFO \
    -e ASCEND_TOOLKIT_HOME="${ASCEND_TOOLKIT_HOME:-/usr/local/Ascend/cann-9.1.0}" \
    ${ENGINE_DOCKER_EXTRA:-} \
    --entrypoint bash \
    "$ENGINE_IMAGE" -c "sleep infinity"
fi

if container_exists "$MOTOR_CONTAINER"; then
  say "跳过已存在的网关容器: $MOTOR_CONTAINER"
else
  say "创建网关容器: $MOTOR_CONTAINER (image=$MOTOR_IMAGE)"
  docker run -d --name "$MOTOR_CONTAINER" \
    --network=host --ipc=host --shm-size=64m \
    -v "${SCENARIO_ROOT}:${SCENARIO_ROOT}" \
    -v "${MODEL_PATH}:${MODEL_PATH}" \
    ${MOTOR_DOCKER_EXTRA:-} \
    --entrypoint bash \
    "$MOTOR_IMAGE" -c "sleep infinity"
fi

say "容器就绪："
docker ps --filter "name=$ENGINE_CONTAINER" --filter "name=$MOTOR_CONTAINER" \
  --format '  {{.Names}}  {{.Image}}  {{.Status}}'
