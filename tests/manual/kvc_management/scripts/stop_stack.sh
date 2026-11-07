#!/usr/bin/env bash
# Stop only the isolated KVC functional test containers (kvcfunc-*).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for name in "$MOTOR_CONTAINER" "$ENGINE_CONTAINER" "$META_CONTAINER"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
        docker rm -f "$name" >/dev/null 2>&1 || true
        log "removed $name"
    else
        log "not present $name"
    fi
done
log "isolated KVC functional stack stopped"
