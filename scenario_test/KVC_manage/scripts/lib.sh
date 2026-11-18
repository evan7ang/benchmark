#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/env/env.sh}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE; copy env/profiles/3.2.sh first" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ENV_FILE"
mkdir -p "$ENGINE_LOG_DIR" "$MOTOR_LOG_DIR" "$ROOT/results"
say(){ echo "[$(date '+%F %T')] $*"; }
arm_cfg(){
  case "$1" in
    off) echo "$MOTOR_CFG_OFF" ;;
    on)  echo "$MOTOR_CFG_ON" ;;
    *) echo "unknown arm: $1" >&2; exit 2 ;;
  esac
}
arm_lc(){ case "$1" in off) echo 0;; on) echo 1;; *) exit 2;; esac; }
