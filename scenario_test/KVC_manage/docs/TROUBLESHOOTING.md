# TROUBLESHOOTING

## prefetch 不增长

确认 AISBench 已包含 commit `c199768`：它保留显式 `resume`，不再折叠成 `running`。
旧代码需要 sidecar；本包不依赖 sidecar。

## p50 不动但 mean 动了

正常。resume 只占全部请求约 12%；p50 是中位数，少数 resume 拉不动中位数。
本项目的交付口径是 **overall mean**。

## control 没有逐请求命中

control 不发 header，引擎日志没有 victim session。使用 on/off 的
`prefix_cache_hits_total` 差值交叉验证；target 则直接解析 `cached_before`。

## edit_fail > 0

当前会看到 4 个失败 edit，来自 attacker 的初始短 pause 行（没有可 offload 的
prefix）。它们不影响 16 个 victim 的 offload/prefetch；若要求严格为 0，需要把
attacker 调度与 KVC 管理隔离。

## 引擎启动报 HBM 不足

确认没有其他实验占用 NPU 0/1；重启引擎前 `docker ps` 和 `npu-smi info` 确认干净。
1200/1600 blocks 都需要先清理残留引擎。
