# DESIGN

## 场景

16 个 victim 会话，每个 8.4k prompt：

```text
cold -> pause(180s) -> resume -> finalize
```

另有 4 个 attacker session，每个先 pause 20s，再发 16 个 unique 8.4k 请求。
attacker 在 victim pause 期间制造 churn，把未管理的 victim prefix KV 挤出 HBM。

容量：

```text
victim 16 × 66 = 1,056 blocks
attacker 64 × 66 = 4,224 blocks
pool = 1,600 blocks
```

## 臂

- `off`：不发 header，KVC lifecycle 关闭；没有 offload/prefetch。
- `on`：发 header + cache_control；`idle_offload_s=1`，`idle_evict_s=0`，
  `prefetch_enabled=true`。resume 时由 AISBench 显式上报 `resume`。

## 指标

- 主指标：overall mean TTFT，全部 132 个 AISBench 请求。
- 辅助：overall p50、victim resume TTFT、attacker p50。
- 机制：victim resume 的引擎日志 `cached_before`；run 级
  `prefix_cache_hits_total / prefix_cache_queries_total`。
- `overall-inference`：cold + resume + attacker，排除 pause/finalize。

`overall` 包含生命周期行，不能单独解释 KVC 收益；reference result 中 mean 降 26.3%，
p50 只降 5.0%，是请求构成造成的。
