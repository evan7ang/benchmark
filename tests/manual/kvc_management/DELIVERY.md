# KVC 管理功能测试包：交付与目标机执行说明

本文面向测试执行人员。测试包与开发机解耦，所有路径、镜像、端口、卡号和模型名都由
`kvc_test.env` 配置。

## 1. 交付物

| 交付物 | 说明 |
|---|---|
| `kvc_manual_test_<version>.tar.gz` | 本测试套件（脚本、文档、配置模板） |
| `ais-runner.tar` | AISBench 运行镜像（由 benchmark 源码构建） |
| `motor.tar` | Motor 镜像（已包含可运行的 Motor） |
| `engine.tar` | vllm/vllm-ascend 镜像（已包含 Agent Hint/KVC 能力） |
| 模型目录 | 目标模型（路径、服务名、max_model_len 可配） |

镜像由交付方用 `docker save` 导出：

```bash
docker save -o ais-runner.tar   <AIS_IMAGE>
docker save -o motor.tar        <MOTOR_IMAGE>
docker save -o engine.tar       <ENGINE_IMAGE>
```

## 2. 目标机前置条件

- Linux + Docker，支持 `--network host`、`--privileged`、Ascend 设备直通。
- 至少一张空闲 NPU；Ascend 驱动版本与交付镜像匹配。
- `python3 >= 3.9`、`curl`、`bash`、`tar`。
- 模型文件已放到目标机。
- 三个镜像已 `docker load`。

## 3. 目标机执行步骤

```bash
# 1. 导入镜像
docker load -i ais-runner.tar
docker load -i motor.tar
docker load -i engine.tar

# 2. 解压测试包
tar xzf kvc_manual_test_<version>.tar.gz
cd kvc_manual_test

# 3. 生成配置（交互式，或直接编辑 kvc_test.env）
./qa_run.sh init

# 4. 编辑配置（至少改 BENCH_SOURCE_DIR / MODEL_PATH / MODEL_NAME / 镜像 tag / CARD）
vi kvc_test.env

# 5. 环境检查
./qa_run.sh doctor

# 6. 一键执行（fast 模式，约 25-35 分钟）
./qa_run.sh all --fast

# 7. 查看报告
# results/QA_REPORT_latest.html
```

### 常用命令

```bash
./qa_run.sh doctor                 # 环境/镜像/模型/端口/卡/版本检查
./qa_run.sh datasets --fast        # 只生成并校验数据集
./qa_run.sh case s3 --fast         # 只跑 pause->offload->prefetch
./qa_run.sh observe                # 实时看 KV usage / Waiting / KVC 计数
./qa_run.sh report                 # 重新生成最近一次报告
./qa_run.sh stop                   # 停止隔离栈
KEEP_STACK=1 ./qa_run.sh all --fast  # 跑完不停止栈，便于排查
```

## 4. 配置说明（`kvc_test.env`）

完整字段见 `kvc_test.env.example`。最关键的：

```bash
BENCH_SOURCE_DIR=/path/to/benchmark   # benchmark 源码目录
MODEL_PATH=/path/to/model
MODEL_NAME=my-model
MAX_MODEL_LEN=32768
CHAT_API=true                 # 模型无 chat template 时设为 false
CARD=0                        # 单卡；多卡用 CARDS=0,1 + TP_SIZE=2

ENGINE_IMAGE=...
MOTOR_IMAGE=...
AIS_IMAGE=...

MOTOR_INSTALL_MODE=image      # image | wheel | source
WORK_DIR=/opt/kvcfunc

ENGINE_PORT=18000
MOTOR_INFER_PORT=19125
MOTOR_MGMT_PORT=19126
MOTOR_OBS_PORT=19127
MMC_META_PORT=15000
MMC_STORE_PORT=16000
MMC_METRICS_PORT=18181
MMC_LOOKUP_PORT=19100

KVC_DATA_MODE=synthetic       # synthetic | legacy | external
```

### 4.1 数据模式

| 模式 | 说明 | 目标机是否需要外部数据 |
|---|---|---|
| `synthetic` | 用模型 tokenizer 现场生成确定性长 prompt，默认 | 否 |
| `legacy` | 使用 `SOURCE_DATA_DIR` 下的 SWE-bench 派生 JSONL | 是 |
| `external` | 使用 `DATA_DIR` 下已存在的同名 JSONL | 是 |

推荐 `synthetic`，不依赖外部 trace，且适配不同 `MAX_MODEL_LEN`。

### 4.2 Motor 安装模式

| 模式 | 说明 |
|---|---|
| `image` | 使用交付的 Motor 镜像（推荐） |
| `wheel` | 安装 `MOTOR_WHEEL` 指定的 wheel |
| `source` | 从 `MOTOR_SOURCE_DIR` 构建；先执行 `./qa_run.sh prepare-motor` |

## 5. 版本兼容性

`doctor` 会检查并写入 `$WORK_DIR/MANIFEST.json`：

- benchmark 源码 commit；
- engine / motor / ais 镜像 ID；
- 模型名、max_model_len、CHAT_API；
- 数据模式和 Motor 安装模式。

必须保证：

1. AISBench 源码/镜像包含 `delay_after_ms`（doctor 会检查）；
2. Motor 镜像包含 KVC lifecycle 代码（doctor 会检查 `import motor`）；
3. engine 镜像支持 Agent Hint / KVC（交付方保证镜像配对）；
4. 模型 tokenizer 能加载，`MAX_MODEL_LEN` 与实际模型一致。

## 6. 常见问题

1. **卡被占用/显存不足**
   `doctor` 会打印每张卡的 HBM 使用率。选择另一张卡：修改 `CARD`，重新 `doctor`。
   engine 启动失败会在 `ENGINE_START_TIMEOUT`（默认 300s）内快速报错，不会等很久。

2. **端口冲突**
   `doctor` 会逐项检查端口。修改 `*_PORT` 配置即可。

3. **`MOTOR_INSTALL_MODE=wheel` 找不到 wheel**
   确认 `MOTOR_WHEEL` 路径存在；或改用 `image` 模式。

4. **`CHAT_API=true` 但模型没有 chat template**
   `doctor`/生成器会报错。把 `CHAT_API=false`，数据集会改用 completions prompt。

5. **AISBench 请求 400**
   合成数据生成器已做 tokenizer 校验和安全余量；如果使用 `external` 数据，先运行
   `./qa_run.sh datasets` 做上下文校验。

6. **finalize 后复用 session 报 `applied=false/terminal`**
   正常。`qa_run.sh` 已在每个 KVC-on 场景前自动重启 Motor。

7. **prefetch 后 TTFT 没改善**
   本套件只验证功能动作；prefetch 是异步提交，不保证数据已回 HBM。

## 7. 本轮解耦验证结果

在一台“模拟新服务器”配置下完整执行：

```text
WORK_DIR=/tmp/kvc_decouple
CARD=6
ENGINE_PORT=18100
MOTOR_INFER_PORT=19225 / MGMT=19226 / OBS=19227
MODEL_NAME=new-kvh
KVC_DATA_MODE=synthetic
```

结果：`all --fast` **6/6 PASS**

| 用例 | 结果 | 关键观测 |
|---|---|---|
| S1 压满基线 | PASS | KV p50 68.5% / max 74.6%，Waiting 7 |
| S2 finalize→evict | PASS | 4 finalize → 4 evict / 360 blocks |
| S3 pause→offload→prefetch | PASS | 3 pause → 3 offload → 3 prefetch，3 evict |
| S5 压满触发 | PASS | KV p50 80.6% / max 98.7%，10 offload / 8 prefetch |
| S6 KVC off | PASS | 生命周期正常，KVC/engine edit 全 0 |
| S7 high-water gate | PASS | gate_skip=2，offload=0 |

说明：该验证没有使用开发机路径、没有使用 `benchmark-data`、没有使用固定镜像 tag，
证明测试工具已与开发机环境解耦。
