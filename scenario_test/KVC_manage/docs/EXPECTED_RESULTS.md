# EXPECTED RESULTS

参考环境：179.169.3.2，Qwen3-8B，Union TP2，1600 blocks，AISBench `7c3b593`。

| 指标 | off | on |
|---|---:|---:|
| overall mean TTFT | 2442.5 ms | **1800.1 ms** |
| overall p50 | 1139.8 ms | 1083.0 ms |
| victim resume p50 | 5010.7 ms | **272.9 ms** |
| victim resume mean | 4774.7 ms | **272.5 ms** |
| victim resume p90 | 8241.7 ms | 286.9 ms |
| victim resume p99 | 8623.2 ms | 366.0 ms |
| victim cold p50 | 8342.0 ms | 8888.5 ms |
| attacker p50 | 1140.8 ms | 1166.5 ms |

变化：

```text
overall mean:        -26.3%
victim resume p50:   -94.6%
```

## Prefix cache

```text
off hits=267,264 / queries=1,076,954
on  hits=400,384 / queries=1,076,954
diff=133,120 token = 1,040 blocks = 16 victims × 65 blocks
victim resume hit rate = 133,120 / (16 × 8,456) = 98.4%
```

target 引擎日志可见 victim resume `cached_before=65`（66 块上限）。

## KVC

```text
offload_total=20（16 victim + 4 attacker 初始 pause）
prefetch_total=16（16 victim resume）
evict_total=16（victim finalize）
queue_full=0
edit_fail=4（attacker 初始 pause，不影响 victim）
```

## 解释

overall p50 不变是因为 resume 只占全部请求约 12%；mean 能反映全部 resume 的收益。
领导最终采用 mean 口径，因此本包不包含为提升 p50 占比而构造的 extreme 数据集。
