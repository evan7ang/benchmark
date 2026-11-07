# S3/S4 pause → offload → resume → prefetch

## 目标
验证 pause 后 idle timer 触发 offload，resume 触发 prefetch，finalize 触发 evict。

## 步骤
```bash
cd kvc_manual_test
bash scripts/start_stack.sh \
  --kvc on --idle-offload 30 --high-water 0 --prefetch on \
  --no-engine --no-meta

bash scripts/run_case.sh \
  --case pause_offload_prefetch \
  --model kvc_manual_c8 \
  --dataset kvc_manual_pause_offload_quick
```

## 通过标准
- `offload_total > 0`，与 pause POST 数匹配；
- `prefetch_total > 0` 且 ≤ `offload_total`；
- `edit_fail=0`、`send_fail=0`；
- engine 有 offload/prefetch edit 和 block 数。

## 常见坑
- **每轮前必须重启 Motor**：finalize 是终态，复用 session_id 会让 pause 返回 `applied=false reason=terminal`。
- prefetch 是异步提交，不保证数据已回 HBM。

## 本轮结果
5 pause → 5 offload → 5 prefetch；4 finalize → 4 evict；2284 blocks；`edit_fail=0`。
证据：`results/20261105_231030_pause_offload_prefetch/`。
