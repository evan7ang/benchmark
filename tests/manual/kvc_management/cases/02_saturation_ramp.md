# S1 压满基线（KVC off）

## 目标
确认单卡 TP1 在 c=8 下能压满 KV，给后续 KVC 场景提供压满证据。

## 步骤
```bash
cd kvc_manual_test
bash scripts/start_stack.sh \
  --kvc off --port 18000 --no-engine --no-meta

bash scripts/run_case.sh \
  --case ramp_kvc_off_pause_c8 \
  --model kvc_manual_c8 \
  --dataset kvc_manual_pause_offload_multi \
  --duration 180
```

## 通过标准
- `engine-metrics.csv` 中 `kv_cache_usage_perc` max ≥ 70%，或
- `num_requests_waiting` max > 0，或
- preemption 计数增长。

## 本轮结果
KV usage p50 72.4%，max 97.4%，Waiting max 6。
证据：`results/20261105_215533_ramp_kvc_off_pause_c8/`。
