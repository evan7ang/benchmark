#!/bin/bash
# Example harness command for `ais_bench.capture run`.
#
# Prerequisites (one-time):
#   - DSH runtime: set DSH_BIN to the CLI JS entry (e.g. .../@deepseek-ai/dsh/lib/bin.js)
#   - DSH_HOME points at a writable home
#   - profile exists and has the hint plugin:
#       $DSH_BIN --profile capture --from-default-profile headless < /dev/null || true
#       $DSH_BIN plugin --profile capture add /path/to/dsh-agent-hints
#
# The capture CLI exports CAPTURE_ENDPOINT (recorder baseURL, .../v1) and
# CAPTURE_LOG. This script writes settings.yaml + patch dynamically and runs a
# two-turn session (turn 2 adopts the turn-1 session id).
set -u
: "${DSH_BIN:?set DSH_BIN to the dsh CLI JS entry}"
: "${DSH_HOME:?set DSH_HOME}"
NODE_BIN="${NODE_BIN:-node}"
PROFILE="${DSH_PROFILE:-capture}"
ENDPOINT="${CAPTURE_ENDPOINT:?CAPTURE_ENDPOINT is required}"
OUT="$(dirname "${CAPTURE_LOG:-./requests.jsonl}")/dsh"
mkdir -p "$OUT"

cat > "$DSH_HOME/settings.yaml" <<EOF
agent-default-model:
  provider: deepseek-hint
  model: ${CAPTURE_MODEL:-kvh}
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
      - id: ${CAPTURE_MODEL:-kvh}
        name: ${CAPTURE_MODEL:-kvh}
        contextWindow: ${CAPTURE_CONTEXT:-40960}
        maxTokens: ${CAPTURE_MAX_TOKENS:-256}
    defaultContextWindow: ${CAPTURE_CONTEXT:-40960}
    defaultMaxTokens: ${CAPTURE_MAX_TOKENS:-256}
EOF

export CAPTURE_API_KEY="${CAPTURE_API_KEY:-dummy}"
cd "$OUT"

"$NODE_BIN" "$DSH_BIN" --profile "$PROFILE" --patch "$OUT/dsh.patch.yml" --json \
  "Reply with exactly OK." > "$OUT/run1.jsonl" 2> "$OUT/run1.stderr"
rc1=$?
SID=$("$NODE_BIN" -e '
const fs=require("node:fs");
for (const line of fs.readFileSync(process.argv[1],"utf8").split("\n")) {
  if (!line.trim()) continue;
  try { const e=JSON.parse(line); if (e.type==="session" && e.sessionId) { process.stdout.write(String(e.sessionId)); break; } } catch {}
}' "$OUT/run1.jsonl")
[ -n "$SID" ] || { echo "no session id from turn 1" >&2; exit 1; }

"$NODE_BIN" "$DSH_BIN" --profile "$PROFILE" --patch "$OUT/dsh.patch.yml" --json --session-id "$SID" \
  "Reply with exactly OK again." > "$OUT/run2.jsonl" 2> "$OUT/run2.stderr"
rc2=$?
echo "dsh two-turn exit=$((rc1 + rc2)) session=$SID out=$OUT"
exit $((rc1 + rc2))
