#!/bin/bash
# Replay one arm of the cache_control A/B and collect three artefacts:
#   obs-before.json / obs-after.json  engine /metrics snapshots (engine port!)
#   pool.csv                          2 s sampler: epoch usage running waiting preempt hits queries cached
#   recs.json                         per-request ttft_ms / total_ms / prompt_tokens / call_index
#
# usage: run_arm.sh <port> <arm> <dataset-abbr> <tag>
#   port          engine port of this lane (metrics are read from the same port)
#   arm           cc | nocc | off
#   dataset-abbr  e.g. v22sAcc
#   tag           run tag, e.g. cold_v22sAcc_a22
#
# env:
#   CCV_CFG_DIR    config dir passed to ais_bench  (holds datasets/ and models/)
#   CCV_RUN_ROOT   host dir that receives one subdir per arm
#   CCV_DATA_MOUNT host dir mounted as /data (dataset shards + xtract.py)
#   CCV_REPO_MOUNT host dir of this repo, mounted as /workspace
#   CCV_MODEL_HOST host dir of the weights, mounted read-only
#   CCV_XTRACT     path (inside the container) of the per-request extractor
#   AIS_IMAGE      client image, default ais-runner:ais-1
#   CCV_JSON_SUFFIX (default "_"): dataset jsonl suffix per arm
set -u
PORT=${1:?port}
ARM=${2:?arm}
DSABBR=${3:?ds-abbr}
TAG=${4:?tag}

CFG=${CCV_CFG_DIR:?CCV_CFG_DIR}
OUT=${CCV_RUN_ROOT:?CCV_RUN_ROOT}
DATA_MOUNT=${CCV_DATA_MOUNT:-/home/t00887700/benchmark-data/agent_replay_v2/ccvisit/ais_pkg}
REPO_MOUNT=${CCV_REPO_MOUNT:?CCV_REPO_MOUNT}
MODEL_HOST=${CCV_MODEL_HOST:-/home/RAG_data/models/Qwen3-8B}
XTRACT=${CCV_XTRACT:-/data/scripts/xtract.py}
IMAGE=${AIS_IMAGE:-ais-runner:ais-1}
PKG=$DATA_MOUNT
# the dataset configs read CCV_DATA_DIR inside the client container
DS_DIR_IN=${CCV_DATA_DIR_IN_CONTAINER:-/data/data}

# /metrics must be read off the engine port.  The proxy in front of the engines
# reports cumulative values in the 1e10 range that are identical across cards
# and therefore useless for arm-to-arm diffs.
METRICS=$PORT

case $ARM in
  cc|nocc) MODEL=${CCV_MODEL_ON:-ccv_on}  ;;
  off)     MODEL=${CCV_MODEL_OFF:-ccv_off} ;;
  *) echo "bad arm $ARM"; exit 2 ;;
esac

DS=ccv_$DSABBR
RUN=$OUT/real_${TAG}_$(date +%m%d_%H%M%S)
mkdir -p $RUN
echo "PORT=$PORT METRICS=$METRICS ARM=$ARM MODEL=$MODEL DS=$DS"
echo "RUN=$RUN"

curl -s -X POST http://127.0.0.1:$PORT/reset_prefix_cache; echo
for try in 1 2 3 4 5 6 7 8 9 10; do
  curl -s --max-time 20 http://127.0.0.1:$METRICS/metrics > $RUN/obs-before.json
  sz=$(wc -c < $RUN/obs-before.json)
  [ "$sz" -gt 5000 ] && break
  echo "  metrics snapshot only ${sz}B, retry $try"
  sleep 5
done
wc -c $RUN/obs-before.json

# ---- pool pressure sampler (2 s) --------------------------------------------
echo "epoch usage running waiting preempt hits queries cached" > $RUN/pool.csv
touch $RUN/.sampling
(
  while [ -f $RUN/.sampling ]; do
    curl -s -m 4 http://127.0.0.1:$METRICS/metrics | awk -v ts="$(date +%s)" '
      /^vllm:kv_cache_usage_perc/        {u=$NF}
      /^vllm:num_requests_running\{/     {r+=$NF}
      /^vllm:num_requests_waiting\{/     {w+=$NF}
      /^vllm:num_preemptions_total/      {p+=$NF}
      /^vllm:prefix_cache_hits_total/    {h+=$NF}
      /^vllm:prefix_cache_queries_total/ {q+=$NF}
      /^vllm:prompt_tokens_cached_total/ {c+=$NF}
      END {printf "%s %.4f %.0f %.0f %.0f %.0f %.0f %.0f\n", ts, u, r, w, p, h, q, c}
    ' >> $RUN/pool.csv
    sleep 2
  done
) &
SAMP=$!

docker rm -f real-$TAG >/dev/null 2>&1
docker run -d --name real-$TAG --network host --entrypoint bash \
  -v $REPO_MOUNT:/workspace \
  -v $DATA_MOUNT:/data \
  -v $MODEL_HOST:$MODEL_HOST:ro \
  -w /workspace -e PYTHONPATH=/workspace -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
  -e CCV_DATA_DIR=$DS_DIR_IN \
  -e CCV_HOST_IP=${CCV_HOST_IP:-127.0.0.1} -e CCV_HOST_PORT=$PORT \
  -e CCV_MODEL_PATH=$MODEL_HOST -e CCV_SERVED_MODEL=${CCV_SERVED_MODEL:-kvh} \
  -e CCV_CONCURRENCY=${CCV_CONCURRENCY:-48} \
  $IMAGE -lc "ais_bench --config-dir $CFG --models $MODEL --datasets $DS --mode perf --num-warmups 0 --work-dir $RUN/ais > $RUN/run.log 2>&1"

echo "polling..."
for i in $(seq 1 900); do
  sleep 5
  ST=$(docker inspect -f '{{.State.Status}}' real-$TAG 2>/dev/null)
  if [ "$ST" != "running" ]; then echo "finished at $((i*5))s status=$ST"; break; fi
done

rm -f $RUN/.sampling
wait $SAMP 2>/dev/null
curl -s --max-time 20 http://127.0.0.1:$METRICS/metrics > $RUN/obs-after.json
echo "=== result table ==="
grep -E 'Duration|Requests|Concurrency|Throughput|Tokens' $RUN/run.log | tail -20
echo "=== xtract ==="
python3 $XTRACT $RUN/ais --json $RUN/recs.json 2>&1 | tail -25

echo "=== hit rate / cached tokens (obs diff) ==="
python3 - <<PY
def g(p):
    d = {}
    for line in open(p):
        for k in ("vllm:prefix_cache_queries_total", "vllm:prefix_cache_hits_total",
                  "vllm:prompt_tokens_cached_total"):
            if line.startswith(k):
                d[k] = d.get(k, 0.0) + float(line.rsplit(" ", 1)[1])
    return d
a, b = g("$RUN/obs-before.json"), g("$RUN/obs-after.json")
qd = b.get("vllm:prefix_cache_queries_total",0) - a.get("vllm:prefix_cache_queries_total",0)
hd = b.get("vllm:prefix_cache_hits_total",0) - a.get("vllm:prefix_cache_hits_total",0)
cd = b.get("vllm:prompt_tokens_cached_total",0) - a.get("vllm:prompt_tokens_cached_total",0)
print("  queries=%.0f hits=%.0f hit rate=%.4f%% cached_prompt_tokens=%.0f" %
      (qd, hd, 100.0*hd/qd if qd else 0, cd))
PY

echo "=== pool pressure (from pool.csv) ==="
awk 'NR>1 && NF==8 {
  n++; u=$2; if (u>maxu) maxu=u; su+=u;
  if ($3>maxr) maxr=$3;
  if ($4>maxw) maxw=$4;
  if ($5>maxp) maxp=$5;
}
END {printf "  samples=%d usage mean=%.1f%% max=%.1f%% | running max=%.0f waiting max=%.0f preemptions max=%.0f\n",
     n, 100.0*su/n, 100.0*maxu, maxr, maxw, maxp}' $RUN/pool.csv

echo "RUN=$RUN"