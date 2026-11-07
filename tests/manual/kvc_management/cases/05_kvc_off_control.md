# S6 KVC off 对照

## 目标
证明 `/agent-status` 生命周期仍正常，但 KVC 关闭时不会产生任何 engine edit。

## 步骤
```bash
cd kvc_manual_test
bash scripts/start_stack.sh \
  --kvc off --no-engine --no-meta

bash scripts/run_case.sh \
  --case kvc_off \
  --model kvc_manual_c1 \
  --dataset kvc_manual_gate_quick
```

## 通过标准
- AISBench 有 pause/running/finalize 的 `/agent-status` 且 HTTP 200；
- `motor:kvc_*` 全部不增长；
- engine 无 edit；
- 请求成功率 100%。

## 本轮结果
5/5 成功；pause/running/finalize 各 1 次；KVC 计数与 engine edit 全 0。
证据：`results/20261105_233822_kvc_off/`。
