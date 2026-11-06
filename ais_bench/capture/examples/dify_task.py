#!/usr/bin/env python3
"""Example harness command for `ais_bench.capture run/batch` (Dify workflow).

Prerequisites:
  - The Dify model provider used by the app points at the recorder endpoint
    (CAPTURE_ENDPOINT) and is the agent-hints provider for hints ON.
  - For batch (which starts a recorder per task), pass a fixed
    `--listen-port` and configure Dify's provider endpoint to that port.
  - Env: DIFY_BASE, DIFY_APP_ID, DIFY_API_KEY; task comes from CAPTURE_PROMPT
    (batch) or DIFY_TASK / DIFY_TASK_FILE.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("DIFY_BASE", "http://localhost").rstrip("/")
APP_ID = os.environ.get("DIFY_APP_ID", "")
API_KEY = os.environ.get("DIFY_API_KEY", "")
TASK = (
    os.environ.get("CAPTURE_PROMPT")
    or os.environ.get("DIFY_TASK")
    or (open(os.environ["DIFY_TASK_FILE"], encoding="utf-8").read() if os.environ.get("DIFY_TASK_FILE") else None)
    or "Reply with exactly OK."
)
USER = os.environ.get("CAPTURE_TASK_ID") or os.environ.get("DIFY_USER") or "capture-%d" % int(time.time())
if not API_KEY:
    sys.exit("DIFY_API_KEY is required")

payload = json.dumps({
    "inputs": {"task": TASK},
    "response_mode": "streaming",
    "user": USER,
}).encode()
req = urllib.request.Request(BASE + "/v1/workflows/run", data=payload, method="POST")
req.add_header("Authorization", "Bearer " + API_KEY)
req.add_header("Content-Type", "application/json")
print("dify app", APP_ID, "base", BASE, "user", USER, "task_bytes", len(TASK.encode()), flush=True)
with urllib.request.urlopen(req, timeout=900) as resp:
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            ev = json.loads(data)
        except Exception:
            continue
        kind = ev.get("event")
        if kind in ("workflow_started", "workflow_finished", "node_finished", "error"):
            d = ev.get("data") or {}
            print(kind, d.get("title") or "", d.get("status") or "", flush=True)
        if kind == "workflow_finished":
            if (ev.get("data") or {}).get("status") == "failed":
                sys.exit(1)
            break
