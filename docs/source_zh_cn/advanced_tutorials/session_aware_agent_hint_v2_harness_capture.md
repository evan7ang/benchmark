# Agent Hint V2 真实 Harness 抓包与数据集构建

本文说明如何使用 `ais_bench.capture` 抓取真实 harness（DSH、Dify 或任意 OpenAI 兼容客户端）的请求，并将其构建为可回放的 Agent Hint V2 数据集。

## 1. 功能定位

`ais_bench.capture` 补齐了 Agent Hint V2 的**上游采集链路**：

```text
真实 harness（DSH / Dify / ...）
  │  hints on：agent-hints provider 发送 X-Agent-Hint
  │  hints off：不发送 hint，使用 harness session header 分组
  ▼
录制代理 recorder.py（记录 header / body / usage / timing）
  ▼
to-model-io（requests.jsonl -> model_io/*.json）
  ▼
build（model_io -> Agent Hint V2 JSONL）
  ▼
已有 V2 回放配置（ON / OFF）
```

它解决的是“只有回放数据、没有真实 harness 请求来源”的问题：

- 保留真实请求的 `X-Agent-Hint`、messages、tools、usage 和时间信息。
- 支持同一批请求分别生成 Hints ON / OFF 数据集，用于控制变量对比。
- 生成的数据集可直接被现有 Agent Hint V2 回放配置消费。

## 2. 组件与命令

模块位置：

```text
ais_bench/capture/
├── recorder.py     # OpenAI 兼容录制代理
├── model_io.py     # requests.jsonl -> model_io
├── v2_builder.py   # model_io -> Agent Hint V2 JSONL
├── cli.py          # 统一 CLI
└── examples/       # DSH / Dify 示例
```

统一入口：

```shell
python -m ais_bench.capture <subcommand>
```

支持的子命令：

| 子命令 | 作用 |
|---|---|
| `run` | 启动录制代理、执行 harness 命令、自动生成 model_io |
| `record` | 只启动录制代理，适合手工接入任意 harness |
| `to-model-io` | 将抓包 JSONL 转换为 model_io 中间文件 |
| `build` | 将 model_io 构建为 Agent Hint V2 JSONL |

运行时建议设置：

```shell
export PYTHONPATH=/path/to/benchmark
```

## 3. 前置条件

### 3.1 上游推理服务

准备 Motor 或 vLLM 的 OpenAI 兼容接口，并确认：

- 上游地址 `UPSTREAM_HOST`
- 上游端口 `UPSTREAM_PORT`
- 服务端实际模型名 `SERVED_MODEL`

录制代理会将 harness 请求中的模型名改写为 `SERVED_MODEL` 后转发。

### 3.2 Harness

harness 必须满足：

- 通过 OpenAI 兼容接口调用模型；
- 请求路径为 `/chat/completions`；
- 支持自定义 base URL，或能被示例脚本包装；
- Hints ON 时，模型 provider 需要发送 `X-Agent-Hint`。

## 4. 快速开始：DSH 抓包

### 4.1 一次性安装

要求 Node >= 22。示例版本为 `@deepseek-ai/dsh@0.1.6-alpha.2`。

```shell
npm install @deepseek-ai/dsh@0.1.6-alpha.2 --ignore-scripts
```

准备 DSH profile 并安装 hint 插件：

```shell
export DSH_BIN=/path/to/node_modules/@deepseek-ai/dsh/lib/bin.js
export DSH_HOME=/path/to/writable/dsh-home

$DSH_BIN --profile capture --from-default-profile headless < /dev/null || true
$DSH_BIN plugin --profile capture add /path/to/dsh-agent-hints
```

说明：

- `dsh plugin add` 需要 Linux 环境可用的 `pnpm`。
- 如果 profile 的 `.npmrc` 需要，设置 `ignore-workspace-root-check=true`。

### 4.2 执行抓包

```shell
cd /path/to/benchmark

PYTHONPATH=/path/to/benchmark \
python -m ais_bench.capture run \
  --harness-cmd "bash ais_bench/capture/examples/dsh_two_turn.sh" \
  --hints on \
  --out captures/dsh_on \
  --upstream-host <motor-host> \
  --upstream-port <motor-port> \
  --upstream-model <served-model> \
  --run-id dsh-on-1 \
  --env DSH_BIN=/path/to/node_modules/@deepseek-ai/dsh/lib/bin.js \
  --env DSH_HOME=/path/to/writable/dsh-home \
  --env CAPTURE_MODEL=<served-model>
```

`run` 会自动完成以下动作：

1. 分配空闲端口并启动录制代理。
2. 向 harness 注入 `CAPTURE_ENDPOINT`、`CAPTURE_LOG`、`HINTS_MODE`、`CAPTURE_RUN_ID`。
3. 执行 harness 命令。
4. 默认自动将抓包结果转换为 `model_io/`。

如需只抓包、不自动转换：

```shell
--no-build-model-io
```

### 4.3 DSH 示例脚本环境变量

`ais_bench/capture/examples/dsh_two_turn.sh` 支持：

| 变量 | 说明 |
|---|---|
| `DSH_BIN` | DSH CLI 的 JS 入口，必填 |
| `DSH_HOME` | 可写的 DSH home，必填 |
| `NODE_BIN` | node 可执行文件，默认 `node` |
| `DSH_PROFILE` | DSH profile 名称，默认 `capture` |
| `CAPTURE_MODEL` | 模型名，默认 `kvh` |
| `CAPTURE_CONTEXT` | 上下文窗口，默认 `40960` |
| `CAPTURE_MAX_TOKENS` | 最大输出 token，默认 `256` |
| `CAPTURE_API_KEY` | 发往录制代理的占位 key，默认 `dummy` |

## 5. 任意 Harness 手工接入

如果 harness 不方便由 `run` 托管，可以拆成 `record` 和 `to-model-io` 两步。

### 5.1 启动录制代理

```shell
PYTHONPATH=/path/to/benchmark \
python -m ais_bench.capture record \
  --listen-host 0.0.0.0 \
  --listen-port 9999 \
  --upstream-host <motor-host> \
  --upstream-port <motor-port> \
  --upstream-model <served-model> \
  --model-id capture-model \
  --log captures/my_run/requests.jsonl
```

### 5.2 配置 harness

将 harness 的 OpenAI base URL 指向：

```text
http://<capture-host>:9999/v1
```

然后正常运行真实任务。录制代理会：

- 转发请求到上游；
- 记录请求 header、body、usage、耗时、状态；
- 解码并记录 `X-Agent-Hint`；
- 将响应原样流式返回。

### 5.3 转换为 model_io

Hints ON：

```shell
python -m ais_bench.capture to-model-io \
  --input captures/my_run/requests.jsonl \
  --out captures/my_run/model_io \
  --hints on \
  --case-id my-task-001
```

Hints OFF：

```shell
python -m ais_bench.capture to-model-io \
  --input captures/my_run/requests.jsonl \
  --out captures/my_run/model_io_off \
  --hints off \
  --session-id my-run-1
```

转换规则：

- 只保留 `POST .../chat/completions` 且 `upstream_status=200` 的请求。
- `--hints on`：只保留真实携带 `X-Agent-Hint` 的推理请求；session-title、compaction 等辅助调用会被丢弃，避免回放时伪造 hint。
- `--hints off`：忽略 hint，优先使用 `x-deepseek-harness-session-id` 分组；没有该 header 时使用 `--session-id`。
- `--session-id` 适合“一次 harness 运行对应一个 session”的场景。

## 6. Dify 抓包

Dify 的模型 provider 配置在其数据库中，因此需要使用固定监听端口。

```shell
PYTHONPATH=/path/to/benchmark \
python -m ais_bench.capture run \
  --harness-cmd "python ais_bench/capture/examples/dify_task.py" \
  --hints on \
  --out captures/dify_on \
  --listen-host 0.0.0.0 \
  --listen-port 9999 \
  --upstream-host <motor-host> \
  --upstream-port <motor-port> \
  --upstream-model <served-model> \
  --run-id dify-on-1 \
  --env DIFY_BASE=http://<dify-host> \
  --env DIFY_APP_ID=<app-id> \
  --env DIFY_API_KEY=<api-key> \
  --env DIFY_TASK="修复这个仓库里的 bug"
```

在 Dify 中将 agent-hints provider 的 base URL 配置为：

```text
http://<capture-host>:9999/v1
```

注意事项：

- 如果 Dify 运行在容器中，`<capture-host>` 必须使用容器可达的宿主机地址，不能写 `127.0.0.1`。
- 录制代理需要 `--listen-host 0.0.0.0`。
- Hints ON / OFF 的切换本质上是 Dify 的 provider 切换：agent-hints provider 或普通 OpenAI-compatible provider。

## 7. 构建 Agent Hint V2 数据集

### 7.1 标准构建

```shell
python -m ais_bench.capture build \
  --input-dir captures/my_run/model_io \
  --output datasets/my_run_v2.jsonl \
  --builder capture \
  --state-mode running \
  --completion-token-cap 256
```

### 7.2 参数说明

| 参数 | 说明 |
|---|---|
| `--input-dir` | `model_io` 目录 |
| `--output` | 输出 JSONL 路径 |
| `--builder capture` | 保留完整 capture hint 字段，推荐用于真实 harness 数据 |
| `--builder jiuwen` | 使用旧版 Jiuwen model_io 转换脚本 |
| `--state-mode running` | 所有 session 保持 running |
| `--state-mode completed-finalize` | 每个 session 最后一条记录的 `agent_status.state` 改为 `finalize` |
| `--default-max-out-len` | 无 usage 时的默认最大输出长度，默认 256 |
| `--completion-token-cap` | 按 usage 推导 `max_out_len` 时的上限，默认 256 |
| `--report` | 自定义报告路径 |

`--builder capture` 会保留以下 hint 字段：

- `session`
- `agent_type`
- `call_index`
- `epoch`
- `priority`
- `cache_control`
- `prediction`
- `agent_status`

缺失时只补充：

- `session.session_id`
- `session.parent_session_id`
- `agent_status.state=running`

### 7.3 输出数据

每条 JSONL 记录包含：

```json
{
  "question": "<case_id>",
  "max_out_len": 256,
  "timestamp": 1760000000000,
  "messages": "[...]",
  "tools": "[...]",
  "agent_hint": {},
  "tool_choice": "auto"
}
```

其中：

- `messages` 和 `tools` 以 JSON 字符串保存，避免 Arrow struct 补 `None`。
- `max_out_len` 来自 capture 的完成 token 数，并受 `--completion-token-cap` 限制。
- `timestamp` 来自抓包时间，用于 `use_timestamp=True` 回放。

## 8. Hints ON / OFF 对比

| 模式 | Harness | 数据集 | 回放 |
|---|---|---|---|
| ON | agent-hints provider（DSH `deepseek-hint`、Dify `agent-hints`） | 保留完整 hint | Header 透传 + `pass_agent_hint=True` |
| OFF | 同一 harness 或普通 OpenAI-compatible provider | 忽略 hint，按 session header 分组 | `pass_agent_hint=False` |

同一份抓包可以直接用不同 `--hints` 参数生成 ON / OFF 数据集，也可以分别抓两次。推荐优先复用同一份抓包，减少负载差异。

回放配置：

- ON：`vllm_api_general_stream_session_hint_v2_trace`
- OFF：`vllm_api_general_stream_session_hint_v2_trace_off`

受控 A/B 对比建议：

```python
use_timestamp=False
```

原因：真实抓包的首条请求可能产生很大的首个时间间隔，影响快速对比。

## 9. 产物与校验

一次完整运行后，目录结构类似：

```text
captures/my_run/
├── requests.jsonl
└── model_io/
    ├── capture_000001.json
    ├── capture_000002.json
    └── capture.report.json

datasets/
├── my_run_v2.jsonl
└── my_run_v2.jsonl.report.json
```

### 9.1 `capture.report.json`

重点关注：

- `records_seen`
- `records_kept`
- `records_skipped`
- `records_skipped_no_hint`
- `sessions`
- `case_id`

### 9.2 数据集报告

重点关注：

- `input_records` / `output_records`
- `skipped`
- `sessions`
- `hint_field_coverage`
- `header_len_max`（限制 16384）
- `max_out_len` 的 min / median / max

建议检查：

1. `hint_field_coverage.session` 接近 100%。
2. `header_len_max` 远小于 16384。
3. `sessions` 与真实 harness 会话数一致。
4. `skipped` 中没有大量非预期请求。

## 10. 注意事项

1. 当前只转换 `/chat/completions` 请求，其他路径会被跳过。
2. `run` 的 `--harness-cmd` 相对路径按当前 shell 工作目录解析，建议从仓库根目录执行或使用绝对路径。
3. `requests.jsonl` 可能包含完整 prompt 和业务数据，需注意存放、脱敏和访问权限。
4. Hints OFF 依赖 `x-deepseek-harness-session-id`；没有该 header 时必须通过 `--session-id` 指定兜底 session。
5. `--state-mode completed-finalize` 是基于每个 session 的最后一条记录派生，不是从 harness 生命周期中真实观测到的 finalize。
6. 已知限制：Motor 当前 `cache_control` 解析器期望 dict，而 V2 Header 规范中是 list 形式，当前会被忽略并打印 WARNING；数据集本身保留原始内容，需要消费端后续适配。
7. capture 数据集使用 `use_timestamp=True` 时可能产生很长的首个延迟，受控对比建议关闭时间戳回放。

## 11. 相关文档

- Agent Hint V2 测评指南：`session_aware_agent_hint_v2.md`
- Agent Hint V2 数据集说明：`session_aware_agent_hint_v2_dataset.md`
- 模块 README：`ais_bench/capture/README.md`


## 12. 真实 DSH 任务批量采集

单个 smoke 任务可使用 `run`。批量真实任务推荐使用远端 `batch` 子命令：
每个 task 独立 recorder、独立 `model_io/`，最终合并为一份 V2 数据集和
manifest。

### 12.1 Task manifest

`tasks.jsonl` 每行一个任务，最少字段：

```json
{"task_id":"django__django-12209","prompt":"修复该仓库中的问题","turns":2}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `task_id` | 任务唯一标识，用于运行目录和日志命名 |
| `prompt` | 首轮任务描述，必填 |
| `turns` | 该任务的轮数，默认取 `--turns` |

### 12.2 DSH 一次性环境

当前验证组合：

```text
DSH:            @deepseek-ai/dsh@0.1.6-alpha.2
DSH_HOME:       /home/t00887700/dsh-home
DSH profile:    capture
DSH plugin:     /home/t00887700/dsh-agent-hints (v0.3.0)
DSH runtime:    /home/t00887700/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js
Node runner:    node:22-bookworm（容器内执行，规避宿主机 Node 动态库问题）
```

初始化 profile 与插件：

```shell
export DSH_HOME=/home/t00887700/dsh-home
export DSH_BIN=/home/t00887700/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js

node "$DSH_BIN" --profile capture --from-default-profile headless < /dev/null || true
printf 'ignore-workspace-root-check=true\n' > "$DSH_HOME/profiles/capture/.npmrc"
node "$DSH_BIN" plugin --profile capture add /home/t00887700/dsh-agent-hints
```

> `dsh plugin add` 需要 Linux 可用的 `pnpm`；在 `node:22-bookworm` 中可先
> `npm install -g pnpm@9.15.0`。

### 12.3 批量运行

```shell
PYTHONPATH=/home/t00887700/benchmark \
python -m ais_bench.capture batch \
  --tasks /home/t00887700/benchmark-data/agent_capture_dsh/tasks.jsonl \
  --harness-cmd "bash ais_bench/capture/examples/dsh_task.sh" \
  --hints on \
  --out /home/t00887700/benchmark-data/agent_capture_dsh/run1 \
  --upstream-host 127.0.0.1 --upstream-port 19325 --upstream-model kvh \
  --default-max-out-len 2048 --completion-token-cap 2048 \
  --env DSH_BIN=/home/t00887700/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js \
  --env DSH_HOME=/home/t00887700/dsh-home \
  --env NODE_BIN=node
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--max-tasks` | 只运行前 N 个任务，便于 smoke |
| `--turns` | task 未指定 `turns` 时的默认轮数 |
| `--allow-failures` | 存在失败 task 时仍返回 0 |
| `--listen-port` | 固定 recorder 端口；Dify 等 provider 需提前配置时使用 |

运行产物：

```text
<out>/
├── runs/<task_id>/
│   ├── requests.jsonl
│   ├── status_events.jsonl
│   └── model_io/
├── model_io/                       # 所有 task 合并后的 model_io
├── dataset_v2.jsonl
├── dataset_v2.jsonl.report.json
└── dataset.manifest.json           # 任务、失败数、sha256、builder 报告
```

`batch` 会把每个 task 的 status sink finalize 应用到该 session 的最后一条请求，
因此 `dataset_v2.jsonl` 中的 `finalize` 来自真实 status 事件，而不是按最后一行推导。

### 12.4 校验数据集

```shell
python -m ais_bench.capture validate \
  --dataset /home/t00887700/benchmark-data/agent_capture_dsh/run1/dataset_v2.jsonl \
  --hints on
```

需要强校验 finalize 时增加：

```shell
--require-finalize
```

### 12.5 回放

同一份 ON 数据集复用于所有对照臂：

```shell
# hints ON
ais_bench --models vllm_api_general_stream_session_hint_v2_dsh_capture_on \
          --datasets dsh_capture_v2 --mode perf

# hints OFF（同一数据集，只是不发 Header/status）
ais_bench --models vllm_api_general_stream_session_hint_v2_dsh_capture_off \
          --datasets dsh_capture_v2 --mode perf
```

`dsh_capture_v2.py` 默认指向
`/home/t00887700/benchmark-data/agent_capture_dsh/current/dataset_v2.jsonl`；
用 symlink 指向本次 run 的数据集，或修改配置路径：

```shell
ln -sfn /home/t00887700/benchmark-data/agent_capture_dsh/<run-id> \
        /home/t00887700/benchmark-data/agent_capture_dsh/current
```

> 注意：当前 `session_aware=True` 的回放循环不消费 `timestamp`，`use_timestamp`
> 只在请求级路径生效。hints on 的受控 A/B 建议保持 `use_timestamp=False`，
> 用 `session_concurrency` ramp；若需要复现真实到达节奏，需要另行给
> session-aware dispatcher 增加时间戳调度。

KVC-off 对照仍使用 ON 模型配置，只关闭 Motor 的
`kvc_lifecycle_config.enabled`。

### 12.6 真实任务注意事项

1. 当前主干 `batch` 不负责解压离线仓库快照、初始化 git baseline 或挂载
   workspace；这些由 harness 脚本或外部任务准备流程完成。离线 SWE-bench
   `workspace_zip` / `--init-git` 能力已从主干移除，后续如有需要应移植进
   `batch.py`，不要长期维护第二套 runner。
2. DSH 的 sandbox 默认 `workspace-write` 需要 bubblewrap/Landlock；生产采集
   请按需改回 `workspace-write` 并安装 sandbox backend，或在隔离容器内使用
   `danger-full-access`。
3. 当前宿主机 Node 缺 `libatomic.so.1`，因此 DSH 在 `node:22-bookworm` 容器内
   执行；vLLM/Motor 必须可从容器 `--network host` 访问。
4. 上游 engine/Motor 重启会导致 `SSE stream ended without [DONE]`；task 会记为
   失败，建议重启后重跑该 task。
5. 真实模型可能把 `max_tokens` 全部用于 thinking；按目标部署设置
   `CAPTURE_MAX_TOKENS` / `CAPTURE_THINKING` / `CAPTURE_REASONING_EFFORT`。
6. DSH 的 `pause`/`running` 是带外 status 事件，不一定出现在请求头。当前 V2
   回放只消费请求头里的 `running`/`resume` 和合并后的 `finalize`；
   `status_events.jsonl` 会保留，但不会按时间重放 `pause`。
7. 真实抓包包含仓库代码和工具输出，注意目录权限、脱敏和保存周期。
