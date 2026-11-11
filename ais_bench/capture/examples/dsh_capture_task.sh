#!/bin/bash
# DSH task harness for `ais_bench.capture batch` with a per-task workspace.
#
# Env supplied by the batch runner:
#   CAPTURE_ENDPOINT, CAPTURE_LOG, CAPTURE_STATUS_LOG
#   CAPTURE_TASK_ID, CAPTURE_PROMPT / CAPTURE_PROMPT_FILE, CAPTURE_TURNS
#   CAPTURE_WORKSPACE (optional), CAPTURE_MAX_TOKENS, CAPTURE_TIMEOUT_S
#   HINTS_MODE=on|off
# Env runtime:
#   DSH_BIN, DSH_HOME, DSH_PROFILE (capture), NODE_BIN (node)
#   DSH_NODE_IMAGE (node:22-bookworm), DSH_MOUNT_ROOT (/home/t00887700)
set -euo pipefail

: "${CAPTURE_ENDPOINT:?CAPTURE_ENDPOINT is required}"
: "${CAPTURE_LOG:?CAPTURE_LOG is required}"
: "${DSH_HOME:?set DSH_HOME}"
: "${DSH_BIN:?set DSH_BIN to the dsh CLI JS entry}"

NODE_IMAGE="${DSH_NODE_IMAGE:-node:22-bookworm}"
PROFILE="${DSH_PROFILE:-capture}"
MODEL="${CAPTURE_MODEL:-kvh}"
CONTEXT="${CAPTURE_CONTEXT:-32768}"
MAX_TOKENS="${CAPTURE_MAX_TOKENS:-2048}"
TIMEOUT="${CAPTURE_TIMEOUT_S:-900}"
TURNS="${CAPTURE_TURNS:-1}"
MOUNT_ROOT="${DSH_MOUNT_ROOT:-/home/t00887700}"
WORKSPACE="${CAPTURE_WORKSPACE:-}"
PROMPT="${CAPTURE_PROMPT:-}"
if [ -z "$PROMPT" ] && [ -n "${CAPTURE_PROMPT_FILE:-}" ] && [ -f "${CAPTURE_PROMPT_FILE}" ]; then
  PROMPT="$(cat "${CAPTURE_PROMPT_FILE}")"
fi
if [ -z "$PROMPT" ]; then
  PROMPT="Reply with exactly OK."
fi

OUT="$(cd "$(dirname "$CAPTURE_LOG")" && pwd)"
STATUS="${CAPTURE_STATUS_LOG:-$OUT/status.jsonl}"
PATCH="$OUT/dsh.patch.yml"
mkdir -p "$OUT"
: > "$STATUS"

cat > "$DSH_HOME/settings.yaml" <<EOF
agent-default-model:
  provider: deepseek-hint
  model: $MODEL
EOF

THINKING_LINE=""
if [ -n "${CAPTURE_THINKING:-}" ]; then
  THINKING_LINE="    thinking: ${CAPTURE_THINKING}"
fi
REASONING_LINE=""
if [ -n "${CAPTURE_REASONING_EFFORT:-}" ]; then
  REASONING_LINE="    reasoningEffort: ${CAPTURE_REASONING_EFFORT}"
fi

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
      - id: $MODEL
        name: $MODEL
        contextWindow: $CONTEXT
        maxTokens: $MAX_TOKENS
    defaultContextWindow: $CONTEXT
    defaultMaxTokens: $MAX_TOKENS
EOF

export CAPTURE_API_KEY="${CAPTURE_API_KEY:-dummy}"
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-$CAPTURE_API_KEY}"
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-$CAPTURE_ENDPOINT}"
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

RUN_CWD="${WORKSPACE:-$OUT}"
WORKDIR="$OUT"

run_turn() {
  local turn="$1" prompt="$2" sid="${3:-}"
  local extra=()
  [ -n "$sid" ] && extra=(--session-id "$sid")
  docker run --rm --network host \
    -v "$MOUNT_ROOT:$MOUNT_ROOT" \
    ${WORKSPACE:+-v "$WORKSPACE:$WORKSPACE"} \
    -w "$RUN_CWD" \
    -e DSH_HOME="$DSH_HOME" \
    -e CAPTURE_API_KEY="$CAPTURE_API_KEY" \
    -e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
    -e DEEPSEEK_BASE_URL="$DEEPSEEK_BASE_URL" \
    -e DSH_PERMISSION_MODE="$DSH_PERMISSION_MODE" \
    "$NODE_IMAGE" \
    timeout "$TIMEOUT" node "$DSH_BIN" --profile "$PROFILE" --patch "$PATCH" --json \
      "${extra[@]}" "$prompt" > "$WORKDIR/run${turn}.jsonl" 2> "$WORKDIR/run${turn}.stderr"
}

turn=1
sid=""
cd "$WORKDIR"
while [ "$turn" -le "$TURNS" ]; do
  if [ "$turn" -eq 1 ]; then
    turn_prompt="$PROMPT"
  else
    turn_prompt="Continue the previous task (turn $turn). Reply with exactly OK."
  fi
  run_turn "$turn" "$turn_prompt" "$sid" && rc=0 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "turn $turn failed (exit $rc)" >&2
    tail -8 "$WORKDIR/run${turn}.stderr" >&2 || true
    exit "$rc"
  fi
  if [ -z "$sid" ]; then
    sid=$(python3 - "$WORKDIR/run1.jsonl" <<'PY' 2>/dev/null || true
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except Exception:
        continue
    if event.get("type") == "session" and event.get("sessionId"):
        print(event["sessionId"], end="")
        break
PY
)
  fi
  turn=$((turn + 1))
done

echo "[dsh-capture-task] task=${CAPTURE_TASK_ID:-?} turns=$TURNS session=${sid:-?} workspace=$RUN_CWD"
