# 坑位清单（都是踩过的）

## 1. `range.unit` 写错 ⇒ 保护范围整段丢失

症状：hint 开了但命中≈0，日志不报错。
规则：`unit` 必须是 `message`；`range.start` 只能缺省或 0；`end` 是消息下标。

## 2. 以为在跑 decl，其实 `hint_sent=false`

`decl.json` 与 `off.json` 字节相同（md5 `a4d44f83…`），唯一差别是请求头。
先看 `requests.jsonl` 的 `hint_b64_len`（decl/life ≈520，off=0），再看 `meta.hint_sent`。

## 3. warmup 的 16,640 不算进分母

两个 warmup 各 8,448 prompt、命中 8,320。`Δhits` 用**原始差**（含这 16,640），
恒等式才对得上；复用块 = `(Δhits − 16,640)/128`。

## 4. `--reset-prefix-cache` 非 200 ⇒ 该轮作废

残留前缀会让首轮命中虚高。`meta.prefix_cache_reset.http_status` 必须是 200。

## 5. banner 对不上 ⇒ 容量结论全歪

必须看到 `GPU KV cache size: <tokens> tokens`：
32GB 卡自然池 = 79,872 tokens；169 override=2031 ⇒ 259,968 tokens。
override 不生效时（出现 2,237 块）直接作废重起。

## 6. motor 重启后忘 `/instances/refresh`

症状：liveness 是 200，但请求 500/超时，`http_errors` 非 0。
`03_start_motor.sh` 已内置 refresh；手工起 coordinator 时别忘。

## 7. 两个 lane 撞共享内存

`SESSION_SHM_KEY` 两 lane 必须不同（A=`ttft52113laneA`，B=`ttft52113laneB`），否则串台。

## 8. 拿 `kv_cache_usage_perc` 当压力信号

本场景 off 臂峰值占用只有 0.11–0.74，从没逼近 1.0，块照样被 pop 干净。
压力指标要看 churn（块/s）与 pause 的比值。

## 9. 改完池子忘了改回

容量扫描后把 `POOL_BLOCKS_OVERRIDE` 恢复成目标值（169=2031；32GB 卡留空），
否则下一轮容量结论全变。

## 10. 中文文档别用会破坏 UTF-8 的工具来回读写

曾把整篇文档写成 `?`（每个汉字吞 2 字节）。用支持 UTF-8 的编辑器/工具，改完校验字节数。

## 11. ★ 镜像 ID 相同 ≠ 容器里的代码相同（resume 被拒的根因）

169 两条 lane 用同一个 motor 镜像，但 B 侧 `site-packages/motor` 是镜像原版、
`coordinator/domain/agent_hint.py` 的状态白名单里**没有 `resume`**：

```python
# 补丁版（正确）
_AGENT_STATUS_STATES_ALLOWED = frozenset({"pause", "resume", "running", "finalize"})
# 镜像原版（B 侧）
_AGENT_STATES_ALLOWED = frozenset({"running", "tool", "pause", "suspend", "finalize"})
```

后果：driver 发的 `{"state":"resume"}` 在 `AgentStatusHint.model_validate` 阶段就被丢掉，
「resume 时重新激活 C 区前缀」这条路径完全死掉，只剩 pause 期间的残余保护 ⇒
hit/pt 腰斩到 **43–48%**（正常应是 72–89%）。

检查（`00_preflight.sh` 已内置）：

```bash
docker exec "$MOTOR_CONTAINER" sh -c \
  "grep -n '_AGENT_STATUS_STATES_ALLOWED\|_AGENT_STATES_ALLOWED' \
   /usr/local/python3.12.13/lib/python3.12/site-packages/motor/coordinator/domain/agent_hint.py"
```

验收时先比 `agent_hint.py` 的 md5/内容，**不要只看 `docker images` 的 ID**。
本包不携带补丁文件；如遇原版，请自行用含 `resume` 白名单的版本覆盖并重启 coordinator。

## 12. 半截证据目录

跑流被 Ctrl-C/中断会留下没有 `meta.json` 的目录。`06_analyze.py`/`07_verify.py` 会自动忽略
（没有 `meta.json` 不算一轮）。不要手改目录内容试图「补全」。

## 13. 端口 19476 返回 404 不是故障

agent-status 先打 mgmt（19476），404 时回落 obs（19477）；**200 才算被接受**。
`meta.json` 里 `state_post.tried` 会记录这条回落路径。

## 14. ★ 相邻臂互相污染（off 基线被 hint 流量劣化）

症状：新鲜引擎上 off 只要 ~186–200 ms；同一引擎先跑过 decl 再跑 off，off 变成 ~1020–1028 ms，
间隔 2.4 h 也不恢复；`reset_prefix_cache` 返回 200 也没用。

原因：hints-on 会在引擎进程内留下会话注册表 / 受保护块 / 虚高空闲计数，重置前缀缓存清不掉。

正确做法：**切臂必须重启引擎进程**（`02_start_engine.sh --restart`），motor 也做容器级重启
（`03_start_motor.sh` 默认已做）。off 轮若检测到同引擎有 hint 历史，V9 判 FAIL，该轮作废。

推论（对外表述）：
- 干净引擎：池子装得下时 hint 是纯开销（+15–20 ms）；
- 混部引擎：hint 会话相对 off 会话有 ~80% 会话级优势；
- 不要把后者说成「hint 让场景提速 80%」。
