#!/bin/bash
# Run several arms of one lane back to back, each on a freshly restarted
# engine, and rotate the arm order per lane so that "the third arm is always
# faster" (instance drift between restarts measured +18 % / -27 %) cannot be
# mistaken for a gain of cache_control.
#
# usage: chain_arms.sh <port> <cards> <pool> <dataset-base> <order> [suffix]
#   e.g. chain_arms.sh 18220 0 2031 v22sA off,nocc,cc _a22
#
# env: MAXLEN MODEL_PATH ENGINE_CONTAINER ENGINE_LOGD CCV_CFG_DIR CCV_RUN_ROOT
#      CCV_DATA_MOUNT CCV_REPO_MOUNT CCV_MODEL_HOST AIS_IMAGE (see run_arm.sh)
set -u
PORT=${1:?port}
CARDS=${2:?cards}
POOL=${3:?pool}
DSB=${4:?dsbase}
ORDER=${5:?order}
SFX=${6:-_x}
TS=$(date +%m%d-%H%M%S)
HERE=$(cd "$(dirname "$0")" && pwd)
LOG=${CCV_CHAIN_LOGD:-/tmp}/chain_${PORT}_${DSB}_${POOL}_${TS}.log
mkdir -p "$(dirname "$LOG")"
echo "=== COLDCHAIN port=$PORT pool=$POOL cards=$CARDS ds=$DSB order=$ORDER sfx=$SFX start $(date +%H:%M:%S) ===" | tee -a $LOG
for arm in $(echo $ORDER | tr ',' ' '); do
  echo "[$(date +%H:%M:%S)] --- cleanlane port=$PORT pool=$POOL card=$CARDS" | tee -a $LOG
  MAXLEN=${MAXLEN:-40960} bash "$HERE/cleanlane.sh" $PORT $POOL $CARDS >> $LOG 2>&1
  RC=$?
  echo "[$(date +%H:%M:%S)] cleanlane rc=$RC" | tee -a $LOG
  if [ $RC -ne 0 ]; then echo "[$(date +%H:%M:%S)] engine not up, abort chain" | tee -a $LOG; break; fi
  echo "[$(date +%H:%M:%S)] --- arm $arm cold" | tee -a $LOG
  bash "$HERE/run_arm.sh" $PORT $arm ${DSB}${arm} cold_${DSB}${arm}${SFX} >> $LOG 2>&1
  echo "[$(date +%H:%M:%S)] cold_${DSB}${arm}${SFX} rc=$?" | tee -a $LOG
done
echo "=== COLDCHAIN DONE $(date +%H:%M:%S) ===" | tee -a $LOG
grep -E 'queries=|usage mean|RUN=' $LOG | tail -12