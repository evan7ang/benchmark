#!/bin/bash
# Single-task, multi-turn DSH capture command for `ais_bench.capture batch`.
# Env (set by the batch runner):
#   CAPTURE_ENDPOINT, CAPTURE_LOG, CAPTURE_TASK_ID, CAPTURE_PROMPT, CAPTURE_TURNS
#   HINTS_MODE=on|off
# Env (runtime):
#   DSH_BIN, DSH_HOME, DSH_PROFILE (default: capture), NODE_BIN (default: node)
set -u
: "${DSH_BIN:?set DSH_BIN}"
: "${DSH_HOME:?set DSH_HOME}"
NODE_BIN="${NODE_BIN:-node}"
PROFILE="${DSH_PROFILE:-capture}"
ENDPOINT="${CAPTURE_ENDPOINT:?CAPTURE_ENDPOINT is required}"
PROMPT="${CAPTURE_PROMPT:-Reply with exactly OK.}"
TURNS="${CAPTURE_TURNS:-1}"
MODEL="${CAPTURE_MODEL:-kvh}"
OUT="$(dirname "${CAPTURE_LOG:-./requests.jsonl}")/dsh"
mkdir -p "$OUT"

cat > "$DSH_HOME/settings.yaml" <<EOF
agent-default-model:
  provider: deepseek-hint
  model: $MODEL
EOF

cat > "$OUT/dsh.patch.yml" <<EOF
- id: agent-hints
  config:
    baseURL: $ENDPOINT
    apiKeyEnv: CAPTURE_API_KEY
    statusSink: $OUT/status.jsonl
    predictionStatic:
      latency_ms: 1000
      output_tokens: 64
      remaining_calls: 3
    models:
      - id: $MODEL
        name: $MODEL
        contextWindow: ${CAPTURE_CONTEXT:-40960}
        maxTokens: ${CAPTURE_MAX_TOKENS:-2048}
    defaultContextWindow: ${CAPTURE_CONTEXT:-40960}
    defaultMaxTokens: ${CAPTURE_MAX_TOKENS:-2048}
EOF

export CAPTURE_API_KEY="${CAPTURE_API_KEY:-dummy}"
cd "$OUT"

turn=1
sid=""
while [ "$turn" -le "$TURNS" ]; do
  if [ "$turn" -eq 1 ]; then
    turn_prompt="$PROMPT"
    extra=()
  else
    turn_prompt="Continue the previous task (turn $turn). Reply with exactly OK."
    extra=(--session-id "$sid")
  fi
  "$NODE_BIN" "$DSH_BIN" --profile "$PROFILE" --patch "$OUT/dsh.patch.yml" --json "${extra[@]}" \
    "$turn_prompt" > "$OUT/run$turn.jsonl" 2> "$OUT/run$turn.stderr"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "turn $turn failed (exit $rc)" >&2
    tail -8 "$OUT/run$turn.stderr" >&2
    exit "$rc"
  fi
  if [ -z "$sid" ]; then
    sid=$("$NODE_BIN" -e '
const fs=require("node:fs");
for (const line of fs.readFileSync(process.argv[1],"utf8").split("\n")) {
  if (!line.trim()) continue;
  try { const e=JSON.parse(line); if (e.type==="session" && e.sessionId) { process.stdout.write(String(e.sessionId)); break; } } catch {}
}' "$OUT/run1.jsonl")
    [ -n "$sid" ] || { echo "no session id from turn 1" >&2; exit 1; }
  fi
  turn=$((turn + 1))
done
echo "dsh task=${CAPTURE_TASK_ID:-?} turns=$TURNS session=$sid out=$OUT"
