# S2 finalize → evict

## 目标
验证 finalize 触发 engine evict、释放 session pin，并且不误触发 offload/prefetch。

## 步骤
```bash
cd kvc_manual_test
bash scripts/start_stack.sh \
  --kvc on --idle-offload 0 --high-water 0 --prefetch off \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case finalize_evict \
  --model kvc_manual_c8 \
  --dataset kvc_manual_finalize_quick
```

## 通过标准
- `evict_total` 与 `state=finalize` POST 数一致；
- `offload_total=0`、`prefetch_total=0`；
- `edit_fail=0`、`send_fail=0`；
- engine 日志有 `Executing edit type=evict` 且 `actual_op_block_num>0`。

## 本轮结果
27 请求全部成功；6 finalize → 6 evict / 985 blocks；`edit_fail=0`。
证据：`results/20261105_215909_finalize_evict/`。
