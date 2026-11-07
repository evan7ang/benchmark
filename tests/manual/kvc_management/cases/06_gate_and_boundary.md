# S7 high-water gate

## 目标
验证 `offload_kv_usage_high_water` 门控：KV 未达阈值时 offload/evict 被跳过。

## 步骤
```bash
cd kvc_manual_test
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 1.0 --prefetch on \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case gate \
  --model kvc_manual_c1 \
  --dataset kvc_manual_gate_quick
```

## 通过标准
- `gate_skip_total > 0`；
- `offload_total=0`、`evict_total=0`；
- `edit_fail=0`。

## 本轮结果
gate_skip=2（pause 与 finalize evict 各跳过一次），offload=0。
证据：`results/20261105_234447_gate/`。
