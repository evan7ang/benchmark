> 示例：开发机“模拟新服务器”解耦验证输出。
> 路径（/tmp/kvc_decouple）和目标机不同，仅用于展示报告格式与判据。

# KVC 管理功能手工测试报告

- 生成时间：2026-11-07 12:30:10
- 测试模式：fast
- 隔离栈：卡 6，engine :18100，Motor :19225-19227
- 用例：6/6 通过

## 总结论：**PASS**

| 用例 | 目的 | 结果 | 关键观测 | 期望 | 证据 |
|---|---|---|---|---|---|
| S1 | 压满基线（KVC off） | PASS | 请求统计：定时截断，未生成完整 details（只看压力/KVC 指标）<br>KVC 计数：全部为 0<br>engine edit：0<br>KV p50=68.5% max=74.6%, Waiting=7, preemptions=0 | KV usage max>=70% 或 Waiting>0 | `20261107_121634_S1` |
| S2 | finalize -> evict | PASS | 请求 16/16 成功<br>status: finalize=4<br>KVC: edit_success_total=4, evict_total=4<br>engine: evict=4, blocks=360<br>KV p50=54.1% max=66.9%, Waiting=3, preemptions=0 | evict 与 finalize 数匹配; offload/prefetch=0; edit_fail=0 | `20261107_121859_S2` |
| S3 | pause -> offload -> prefetch | PASS | 请求 15/15 成功<br>status: finalize=3, pause=3, running=3<br>KVC: edit_success_total=9, evict_total=3, offload_total=3, prefetch_total=3<br>engine: evict=3, offload=3, prefetch=3, blocks=786<br>KV p50=40.6% max=55.0%, Waiting=0, preemptions=0 | offload>0; prefetch>0 且 <=offload; edit_fail=0 | `20261107_122116_S3` |
| S5 | 压满触发（high_water=0.7） | PASS | 请求统计：定时截断，未生成完整 details（只看压力/KVC 指标）<br>status: finalize=1, pause=10, running=9<br>KVC: edit_success_total=18, gate_skip_total=2, offload_total=10, prefetch_total=8<br>engine: offload=10, prefetch=8, blocks=2322<br>KV p50=80.6% max=98.7%, Waiting=7, preemptions=0 | KV usage max>=70%; offload>0; edit_fail=0 | `20261107_122408_S5` |
| S6 | KVC off 对照 | PASS | 请求 5/5 成功<br>status: finalize=1, pause=1, running=1<br>KVC 计数：全部为 0<br>engine edit：0<br>KV p50=0.0% max=6.1%, Waiting=0, preemptions=0 | status 200; KVC 计数=0; engine edit=0 | `20261107_122732_S6` |
| S7 | high-water gate | PASS | 请求 5/5 成功<br>status: finalize=1, pause=1, running=1<br>KVC: gate_skip_total=2<br>engine edit：0<br>KV p50=0.0% max=6.1%, Waiting=0, preemptions=0 | gate_skip>0; offload=0; evict=0 | `20261107_122858_S7` |

## 逐项详情

### S1 压满基线（KVC off）

- 结果：**PASS**
- 期望：KV usage max>=70% 或 Waiting>0
- 观测：请求统计：定时截断，未生成完整 details（只看压力/KVC 指标）
- 观测：KVC 计数：全部为 0
- 观测：engine edit：0
- 观测：KV p50=68.5% max=74.6%, Waiting=7, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_121634_S1`

### S2 finalize -> evict

- 结果：**PASS**
- 期望：evict 与 finalize 数匹配; offload/prefetch=0; edit_fail=0
- 观测：请求 16/16 成功
- 观测：status: finalize=4
- 观测：KVC: edit_success_total=4, evict_total=4
- 观测：engine: evict=4, blocks=360
- 观测：KV p50=54.1% max=66.9%, Waiting=3, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_121859_S2`

### S3 pause -> offload -> prefetch

- 结果：**PASS**
- 期望：offload>0; prefetch>0 且 <=offload; edit_fail=0
- 观测：请求 15/15 成功
- 观测：status: finalize=3, pause=3, running=3
- 观测：KVC: edit_success_total=9, evict_total=3, offload_total=3, prefetch_total=3
- 观测：engine: evict=3, offload=3, prefetch=3, blocks=786
- 观测：KV p50=40.6% max=55.0%, Waiting=0, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_122116_S3`

### S5 压满触发（high_water=0.7）

- 结果：**PASS**
- 期望：KV usage max>=70%; offload>0; edit_fail=0
- 观测：请求统计：定时截断，未生成完整 details（只看压力/KVC 指标）
- 观测：status: finalize=1, pause=10, running=9
- 观测：KVC: edit_success_total=18, gate_skip_total=2, offload_total=10, prefetch_total=8
- 观测：engine: offload=10, prefetch=8, blocks=2322
- 观测：KV p50=80.6% max=98.7%, Waiting=7, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_122408_S5`

### S6 KVC off 对照

- 结果：**PASS**
- 期望：status 200; KVC 计数=0; engine edit=0
- 观测：请求 5/5 成功
- 观测：status: finalize=1, pause=1, running=1
- 观测：KVC 计数：全部为 0
- 观测：engine edit：0
- 观测：KV p50=0.0% max=6.1%, Waiting=0, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_122732_S6`

### S7 high-water gate

- 结果：**PASS**
- 期望：gate_skip>0; offload=0; evict=0
- 观测：请求 5/5 成功
- 观测：status: finalize=1, pause=1, running=1
- 观测：KVC: gate_skip_total=2
- 观测：engine edit：0
- 观测：KV p50=0.0% max=6.1%, Waiting=0, preemptions=0
- 证据目录：`/tmp/kvc_decouple/results/20261107_122858_S7`

## 人工观察入口

```bash
# 实时观察 KV usage / Waiting / KVC 计数
./qa_run.sh observe

# 单看某个用例的自动结论
cat <证据目录>/summary.txt
```

## 判定口径

- S1：KV usage max ≥70%，或 Waiting>0，或出现 preemption。
- S2：finalize 数 = evict 数；无 offload/prefetch；`edit_fail=0`。
- S3：offload>0，prefetch>0 且 ≤ offload；`edit_fail=0`。
- S5：KV 压满且 offload>0；`gate_skip` 可为非 0（自限正常）。
- S6：`/agent-status` 200；KVC 计数与 engine edit 全 0。
- S7：`gate_skip>0`，offload=0、evict=0。
