#!/usr/bin/env python3
"""Dify + agent-hints 0.1.7 + hint service capture example.

The plugin itself emits session/priority/cache_control/agent_status. Fields
that come from the hint service (agent_type / prediction / call_index) are
seeded as static entries keyed by the node's content prefix; the companion
driver binds the workflow run id + lifecycle state per request.

Env:
  DIFY_BASE, DIFY_APP_ID, DIFY_API_KEY, DIFY_DSL_FILE
  HINT_SERVICE_URL (e.g. http://127.0.0.1:9200)
  HINT_ENTRIES  JSON list: [{"node_id": "organize", "hint": {"session": {...}, "prediction": {...}}}]
  CAPTURE_PROMPT (optional), CAPTURE_STATUS_LOG (optional finalize sink)
  PYTHONPATH must include the agent-hints repo (for integrations.dify).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from integrations.dify import DifyWorkflowDriver, load_dsl_info

BASE = os.environ.get("DIFY_BASE", "http://localhost").rstrip("/")
APP_ID = os.environ.get("DIFY_APP_ID", "")
API_KEY = os.environ.get("DIFY_API_KEY", "")
DSL = os.environ.get("DIFY_DSL_FILE", "")
HINT_URL = os.environ.get("HINT_SERVICE_URL", "").rstrip("/")
ENTRIES = json.loads(os.environ.get("HINT_ENTRIES", "[]"))
PROMPT = os.environ.get("CAPTURE_PROMPT", "整理站会纪要并输出行动项")
STATUS_LOG = os.environ.get("CAPTURE_STATUS_LOG", "")

if not (APP_ID and API_KEY and DSL and HINT_URL):
    sys.exit("DIFY_APP_ID / DIFY_API_KEY / DIFY_DSL_FILE / HINT_SERVICE_URL are required")

prefixes, _, _ = load_dsl_info(DSL)
entries = []
for item in ENTRIES:
    node_id = str(item.get("node_id") or "")
    prefix = prefixes.get(node_id)
    if prefix and isinstance(item.get("hint"), dict):
        entries.append({"prefix_id": prefix, "hint": item["hint"]})
if entries:
    req = urllib.request.Request(HINT_URL + "/v1/hints",
        data=json.dumps({"entries": entries}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        print("hint_service seed", r.status, r.read().decode()[:200])

driver = DifyWorkflowDriver(base=BASE, api_key=API_KEY, app_id=APP_ID,
                            dsl_file=DSL, hint_service_url=HINT_URL)
result = driver.run(PROMPT, user=os.environ.get("CAPTURE_TASK_ID", "capture"), auto_pass=True)
print("dify run", result.get("run_id"), result.get("status"))
run_id = result.get("run_id")
if STATUS_LOG and run_id:
    finalize = next((e for e in result.get("events") or [] if e.get("state") == "finalize"), None)
    if finalize:
        os.makedirs(os.path.dirname(os.path.abspath(STATUS_LOG)), exist_ok=True)
        with open(STATUS_LOG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"session": {"session_id": run_id}, "agent_status": finalize}) + "\n")
