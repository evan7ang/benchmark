#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
for ARM in off on; do
  say "=== $ARM ==="
  bash "$ROOT/scripts/02_start_engine.sh" "$ARM"
  bash "$ROOT/scripts/03_start_motor.sh" "$ARM"
  bash "$ROOT/scripts/04_run_one.sh" "$ARM"
done
say "matrix done"
