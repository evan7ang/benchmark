#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
say "expect existing engine/motor containers: $ENGINE_CONTAINER / $MOTOR_CONTAINER"
docker inspect "$ENGINE_CONTAINER" >/dev/null || { echo "missing engine container" >&2; exit 1; }
docker inspect "$MOTOR_CONTAINER" >/dev/null || { echo "missing motor container" >&2; exit 1; }
echo OK
