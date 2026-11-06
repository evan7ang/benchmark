#!/usr/bin/env python3
"""Convert captured chat-completions requests into model_io-style JSON files.

Hints ON keeps only requests that actually carried a real X-Agent-Hint
(auxiliary session-title/compaction calls are excluded: the v2 header protocol
would otherwise synthesize a hint the harness never sent). Hints OFF keeps all
inference calls and groups them by the harness session header.

Output matches the fields consumed by
``build_agent_dataset_v2_from_model_io.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def convert(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    seq = 0
    kept = 0
    skipped = 0
    skipped_no_hint = 0
    sessions = set()
    case_id = args.case_id
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            seq += 1
            try:
                rec = json.loads(line)
            except Exception:
                skipped += 1
                continue
            if not str(rec.get("path", "")).rstrip("/").endswith("/chat/completions"):
                skipped += 1
                continue
            if rec.get("upstream_status") != 200:
                skipped += 1
                continue
            body = rec.get("body") or {}
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                skipped += 1
                continue
            hint = rec.get("hint") or {}
            hint_session = (hint.get("session") or {}) if isinstance(hint, dict) else {}
            headers = rec.get("headers") or {}
            if args.hints == "on":
                sid = hint_session.get("session_id")
                parent = hint_session.get("parent_session_id") or sid
                if not sid:
                    skipped_no_hint += 1
                    continue
            else:
                sid = headers.get("x-deepseek-harness-session-id") or args.session_id
                parent = sid
                if not sid:
                    skipped += 1
                    continue
            usage = rec.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            request = {
                "messages": messages,
                "model": body.get("model"),
                "stream": body.get("stream"),
            }
            for key in ("tools", "tool_choice", "temperature", "top_p", "seed", "enable_thinking"):
                if key in body:
                    request[key] = body[key]
            if args.hints == "on":
                # Preserve the full captured AgentHint (nested session,
                # priority, cache_control, prediction, agent_status).
                full_hint = dict(hint) if isinstance(hint, dict) else {}
                sess = full_hint.setdefault("session", {})
                sess.setdefault("session_id", sid)
                sess.setdefault("parent_session_id", parent)
                full_hint.setdefault("agent_status", {"state": "running"})
                request["agent_hint"] = full_hint
            record = {
                "schema_version": "harness-capture-v1",
                "case_id": case_id or body.get("user") or "capture",
                "ordinal": seq,
                "global_request_ordinal": seq,
                "request_kind": "inference",
                "session_id": sid,
                "parent_session_id": parent,
                "request_purpose": "agent_inference",
                "trace_status": "completed",
                "terminal_reason": "completed",
                "timing": {"source_utc": {"start_time": int((rec.get("ts") or 0) * 1000)}},
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
                "request": request,
            }
            path = os.path.join(args.out, f"capture_{seq:06d}.json")
            with open(path, "w", encoding="utf-8") as out:
                json.dump(record, out, ensure_ascii=False, separators=(",", ":"))
            kept += 1
            sessions.add(sid)
    report = {
        "input": args.input,
        "out": args.out,
        "hints": args.hints,
        "records_seen": seq,
        "records_kept": kept,
        "records_skipped": skipped,
        "records_skipped_no_hint": skipped_no_hint,
        "sessions": len(sessions),
        "case_id": case_id,
        "session_id_fallback": args.session_id,
    }
    with open(os.path.join(args.out, "capture.report.json"), "w", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="capture JSONL -> model_io JSON files")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hints", choices=["on", "off"], default="on")
    ap.add_argument("--case-id", default=None)
    ap.add_argument("--session-id", default=None,
                    help="fallback session id when the capture carries no hint/header (one harness run = one session)")
    args = ap.parse_args()
    return convert(args)


if __name__ == "__main__":
    sys.exit(main())
