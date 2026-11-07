#!/bin/bash
# Minimal DSH capture harness for `ais_bench.capture run`.
#
# It runs DSH inside a Node container (the host Node may be unusable), points
# the `deepseek-hint` provider at the recorder endpoint exported by
# `ais_bench.capture`, and writes the out-of-band status events to a local
# JSONL sink next to requests.jsonl.
set -euo pipefail

: "${CAPTURE_ENDPOINT:?CAPTURE_ENDPOINT is required}"
: "${CAPTURE_LOG:?CAPTURE_LOG is required}"
: "${DSH_HOME:?set DSH_HOME}"
: "${DSH_BIN:?set DSH_BIN to the dsh CLI JS entry}"

NODE_IMAGE="${DSH_NODE_IMAGE:-node:22-bookworm}"
PROFILE="${DSH_PROFILE:-capture}"
CAPTURE_MODEL="${CAPTURE_MODEL:-kvh}"
CAPTURE_CONTEXT="${CAPTURE_CONTEXT:-40960}"
CAPTURE_MAX_TOKENS="${CAPTURE_MAX_TOKENS:-256}"
CAPTURE_API_KEY="${CAPTURE_API_KEY:-dummy}"
DSH_MOUNT_ROOT="${DSH_MOUNT_ROOT:-/home/t00887700}"
THINKING_LINE=""
if [ -n "${CAPTURE_THINKING:-}" ]; then
  THINKING_LINE="    thinking: ${CAPTURE_THINKING}"
fi
REASONING_LINE=""
if [ -n "${CAPTURE_REASONING_EFFORT:-}" ]; then
  REASONING_LINE="    reasoningEffort: ${CAPTURE_REASONING_EFFORT}"
fi
OUT="$(cd "$(dirname "$CAPTURE_LOG")" && pwd)"
WORK="$OUT/dsh_work"
STATUS="$OUT/status.jsonl"
PATCH="$OUT/dsh.patch.yml"
mkdir -p "$WORK"
: > "$STATUS"

cat > "$DSH_HOME/settings.yaml" <<EOF
agent-default-model:
  provider: deepseek-hint
  model: $CAPTURE_MODEL
EOF

cat > "$PATCH" <<EOF
- id: agent-hints
  config:
    baseURL: $CAPTURE_ENDPOINT
    apiKeyEnv: CAPTURE_API_KEY
$THINKING_LINE
$REASONING_LINE
    statusSink: $STATUS
    predictionStatic:
      latency_ms: 1000
      output_tokens: 64
      remaining_calls: 3
    models:
      - id: $CAPTURE_MODEL
        name: $CAPTURE_MODEL
        contextWindow: $CAPTURE_CONTEXT
        maxTokens: $CAPTURE_MAX_TOKENS
    defaultContextWindow: $CAPTURE_CONTEXT
    defaultMaxTokens: $CAPTURE_MAX_TOKENS
EOF

docker run --rm --network host \
  -v "$DSH_MOUNT_ROOT:$DSH_MOUNT_ROOT" \
  -w "$WORK" \
  -e DSH_HOME="$DSH_HOME" \
  -e CAPTURE_API_KEY="$CAPTURE_API_KEY" \
  "$NODE_IMAGE" \
  node "$DSH_BIN" --profile "$PROFILE" --patch "$PATCH" --json \
  "Reply with exactly OK."

echo "[dsh-capture-smoke] status=$STATUS workspace=$WORK"
