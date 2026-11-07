#!/usr/bin/env bash
# Create a portable KVC manual-test tarball (no local env, results, or data).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

VERSION="${KVC_TEST_VERSION:-$(date +%Y%m%d)}"
OUT_DIR="${BUNDLE_OUT_DIR:-$KIT_DIR/dist}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/kvc_manual_test_${VERSION}.tar.gz"
printf '%s\n' "$VERSION" > "$KIT_DIR/VERSION"

tar -C "$BENCH_DIR/tests" -czf "$OUT" \
    --exclude='manual/kvc_management/results' \
    --exclude='manual/kvc_management/work' \
    --exclude='manual/kvc_management/kvc_test.env' \
    --exclude='manual/kvc_management/dist' \
    --exclude='manual/kvc_management/configs/legacy' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --transform 's,^manual/kvc_management,kvc_manual_test,' \
    manual/kvc_management

cat <<EOF
bundle: $OUT
version: $VERSION

Deliver these separately (docker save/load):
  $DOCKER_BIN save -o ais-runner.tar   $AIS_IMAGE
  $DOCKER_BIN save -o motor.tar        $MOTOR_IMAGE
  $DOCKER_BIN save -o engine.tar       $ENGINE_IMAGE

Also deliver the model directory and see DELIVERY.md for the target steps.
EOF
