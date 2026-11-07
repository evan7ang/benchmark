# S0 预检

## 目标
确认镜像、模型、wheel、数据集、端口与 NPU 状态可用。

## 命令
```bash
cd kvc_manual_test
bash scripts/preflight.sh
```

## 通过标准
- 三条 `OK image`：motor / engine / ais-runner；
- `OK file`：Motor wheel、模型配置、全量 pause/finalize 数据；
- NPU 有至少一张空闲卡（默认使用卡 1）；
- 服务端点未启动时只显示 WARN，不阻塞首次启动。

## 备注
- 本套件不会操作 `agentx-*` 性能测试容器；
- 如端口被占用，用 `--card/--port` 或环境变量覆盖。
