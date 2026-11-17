# replay_driver.py（vendored 副本）

## 来源与凭据

| 项 | 值 |
|---|---|
| 原始位置 | `179.169.2.2:/home/t00887700/benchmark-data/agent_replay_v2/pause_sweep/replay_driver.py` |
| 原始 sha256 | `f0505286902bc60b2b8bc8c95742b4bfa86fe1fd4df49065c39d8e94630b7366` |
| 版本串 | `v3-anchor-pause-kvtick` |
| 本副本 sha256 | `8d09f5f25313de04630a839870da54975a9cf8dafaf886abe0d01629e7eba915` |
| 依赖 | Python 3 标准库（无第三方包） |

## 与原版的差异（仅「环境解耦」，不动统计口径）

1. **路径契约**：`SHARED` / `DATA_DIR` / `MANIFEST` 改为读环境变量
   `AH_EVIDENCE_ROOT` / `AH_DATA_DIR`（缺省回退到包内 `results/`、`data/generated/`）。
2. **新增 `--out-root`**：证据输出根目录，优先于 `AH_EVIDENCE_ROOT`。
   证据布局：`<out-root>/<dataset>_<arm>_<ts>/{meta.json,requests.jsonl,metrics_before.json,metrics_after.json}`。
3. **lane 表环境化**：端口与容器名由 `AH_{A,B}_{INFER,MGMT,OBS,ENGINE}_PORT`、
   `AH_{A,B}_MOTOR_CONTAINER` 注入；共享内存 key 由 `AH_{A,B}_SHM_KEY` 注入。
4. **模型名环境化**：`AH_MODEL_NAME`（默认 `kvh`）。
5. **臂自检兜底**：宿主机 `/proc` 看不到 coordinator（PID 命名空间隔离）时，
   自动退回 `docker exec <motor> ... /proc/<pid>/environ`；仍取不到则 `selfcheck_usable=false`
   （meta 里可见），不会静默把臂标错。

**未改动**：调度逻辑（绝对时间戳）、warmup（2 条 × 8 token）、TTFT/p50 口径、
`meta.json` 字段、`metrics_before/after` 快照格式、`--anchor pause` 语义。
因此本副本的产物与历史 evidence 可直接对比。

## 常用参数

```bash
--dataset  <name|path>   # data/generated 下按名解析，或直接给 jsonl 绝对路径
--arm      decl|off|life # decl=发 header+lc0；off=不发 header+lc0；life=发 header+lc1
--lane     A|B
--anchor   pause         # 以第一个 pause 状态 POST 落地为时间锚
--reset-prefix-cache     # 跑前 POST 引擎 /reset_prefix_cache（必须 200）
--kvc-ticks              # 每秒采样 KV/排队/抢占，写入 meta.kvc_ticks
--hint-mode header       # header 版 cache_control（X-Agent-Hint）
--repeat   N             # 标注用
--out-root <dir>         # 证据输出根
--dry-run                # 不发请求，只校验峰值并发
```
