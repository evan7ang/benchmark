# agent_hint: the cache_control A/B on real agent-session traces

This directory holds the harness for the question *"does `cache_control` (session
pinning, a.k.a. keep-alive) actually lower TTFT on a realistic agent workload,
and by how much?"* -- plus the artefacts needed to reproduce the 2026-11-19 V22
run, where the answer was **yes: mean TTFT -21.8 % pooled, >= 10 % on all four
cards** ([full report](docs/2026-11-19_V22_实验报告.md)).

## Glossary: cc / nocc / off

Three arms, identical prompt content, two switches apart:

| arm | data file | client config | `agent_hint` sent | `cache_control` | isolates |
|---|---|---|---|---|---|
| **`cc`** | `*_cc.jsonl` | [`configs/models/ccv_on.py`](configs/models/ccv_on.py) | yes | **yes** | the feature under test: session-aware pinning |
| **`nocc`** | `*_nocc.jsonl` | [`configs/models/ccv_on.py`](configs/models/ccv_on.py) | yes | no | `cc` minus `cache_control` => the pinning effect alone |
| **`off`** | `*_nocc.jsonl` | [`configs/models/ccv_off.py`](configs/models/ccv_off.py) | **no** | no | the feature off entirely => true baseline |

`cc` = **c**ache_**c**ontrol, `nocc` = **n**o **c**ache_**c**ontrol, `off` = feature off.
`_cc.jsonl` and `_nocc.jsonl` are byte-identical apart from the `cache_control`
blocks, and `nocc`/`off` share the same file, so the only moving parts are (a)
the field in the request and (b) whether the client transports the envelope.

Why all three: `cc` vs `off` alone cannot tell "the field" from "the pinning",
and `cc` vs `nocc` alone gives no absolute baseline. Every number in the report
is `cc` against the **same lane, same round** mean of `off`/`nocc`.

## Other terms used here

| term | meaning |
|---|---|
| pin / keep-alive | the engine removes the blocks named by `cache_control` from the LRU candidates for `ttl` seconds, so idle sessions do not lose their prefix |
| resume request | `call_index >= 2`; its prefix is the whole previous turn, so it pays a full re-prefill iff the prefix was evicted |
| first-turn | `call_index == 1`; cold by construction, used as an in-run control |
| churn | one-shot requests from "another tenant"; they never carry `cache_control` and act as an independent control |
| oversubscription | sum of live session contexts > pool blocks. Without it nothing is evicted and both arms are identical |
| Φ / e / ρ / κ | protected token share / baseline eviction rate of those tokens / hit still pays the new segment / queueing term |

## Quickstart

```bash
# 0) one-time: generate the four shards (deterministic, stdlib only)
export OUT_DIR=<DATA_DIR>
export VARIANT=V22 SHARDS=4 N_SESS=8 N_PROTECT=5 TURNS=9 TURNS_BASE=30000 TURNS_MAX=39000
export GAP_LO=30 GAP_HI=240 JITTER_MAX=120 N_CHURN=25 CHURN_TOK=20000 CHURN_GAP=1.2 PROT_TTL=300 SHARED=2000
for i in 0 1 2 3; do SHARD=$i python3 tests/agent_hint/data/gen_real.py; done

# 1) point the configs at the data and at the engine
export CCV_DATA_DIR=<DATA_DIR>
export CCV_MODEL_PATH=<MODEL_PATH> CCV_HOST_PORT=<PORT> MODEL_PATH=<MODEL_PATH>
export ENGINE_CONTAINER=<container with the engine> ENGINE_LOGD=<LOG_DIR>
export CCV_CFG_DIR=$PWD/tests/agent_hint/configs
export CCV_RUN_ROOT=<RUN_DIR> CCV_DATA_MOUNT=<DATA_MOUNT> CCV_REPO_MOUNT=$PWD
export CCV_DATA_DIR_IN_CONTAINER=/data/data   # where the shards sit inside the client container

# 2) three arms per lane, arm order rotated, cold engine before each arm
bash tests/agent_hint/scripts/chain_arms.sh <PORT> <CARD> <POOL> v22sA off,nocc,cc

# 3) table: per-arm rows + cc versus off/nocc
python3 tests/agent_hint/report_arms.py <RUN_DIR>/real_cold_v22s*
```

## Files

| path | what it is |
|---|---|
| [`data/gen_real.py`](data/gen_real.py) | trace generator: zero pause rows (idle is `delay_after_ms`), context accumulating 30 k -> 39 k per turn, 8 sessions per instance (1.2x pool), churn tenant, `cache_control` on N_PROTECT sessions only |
| [`configs/datasets/`](configs/datasets) | 12 dataset configs: 4 lanes x `cc`/`nocc`/`off`. `off` and `nocc` point at the same `_nocc` file |
| [`configs/models/ccv_on.py`](configs/models/ccv_on.py) / [`ccv_off.py`](configs/models/ccv_off.py) | hint on / off; model path, host and port come from env |
| [`scripts/cleanlane.sh`](scripts/cleanlane.sh) | cold restart of one engine instance (SIGTERM first, wait for exit, relaunch, wait for `/health`, print `num_gpu_blocks`) |
| [`scripts/run_arm.sh`](scripts/run_arm.sh) | replay one arm, sampling engine `/metrics` before/after plus a 2 s pool sampler and per-request `recs.json` |
| [`scripts/chain_arms.sh`](scripts/chain_arms.sh) | several arms on one lane with a cold engine between them |
| [`report_arms.py`](report_arms.py) | per-arm table and cc-vs-baseline summary |
| [`report_perf.py`](report_perf.py) | the older send-side/perf report tool |
| [`run_perf_ab.sh`](run_perf_ab.sh), [`run_agent_hint_ci.sh`](run_agent_hint_ci.sh), [`run_send_side_conformance.sh`](run_send_side_conformance.sh) | CI-style checks for the send side |
| [`data_manifest.py`](data_manifest.py) | inventory + sha256 of the data assets (`python3 data_manifest.py`) |
| [`docs/2026-11-19_V22_实验报告.md`](docs/2026-11-19_V22_实验报告.md) | the V22 report: dataset spec, profile, procedure, 12-arm results, attribution |

## Pitfalls this harness exists to avoid

1. **Do not use pause requests to model idle time.** They are real requests and
   they refresh the LRU / renew the TTL, which silently keeps the baseline arm's
   prefix alive -- that is how earlier runs produced two arms with bit-identical
   hit counts.
2. **Restart the engine between arms.** `POST /reset_prefix_cache` answers
   `{"success": false}` on this build; a warm start gave 99.4994 % hit rate
   instead of the cold 94.3862 %.
3. **Compare only within a lane, and rotate the arm order.** Restart-to-restart
   drift was measured at +18 % / -27 %; a fixed order turns that into a fake gain.
4. **Do not compare hit *rate* across arms.** The protection path inflates
   `prefix_cache_queries_total` by ~3.3x. Use `prompt_tokens_cached_total`
   (`cached_tok`) and client-side `ttft_ms`; `kv_cache_usage_perc` measures held
   blocks (pinned + in flight), not cache residency.
5. **Do not protect more sessions than the pool can hold.** Pinned blocks are
   neither allocatable nor reclaimable; 5 of 8 sessions (75 % of the pool) is the
   measured safe ceiling, 6 (91 %) deadlocked the engine three times.