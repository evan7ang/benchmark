#!/usr/bin/env python3
"""Build an Agent Hint v2 dataset from harness-capture model_io files.

Unlike the Jiuwen converter, this one PRESERVES the captured AgentHint fields
(session/agent_type/call_index/epoch, priority, cache_control, prediction,
agent_status). It only fills session_id/parent_session_id and a conservative
running state when the capture omitted them.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import statistics
import sys


def header_len(hint) -> int:
    raw = json.dumps(hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def build(args) -> int:
    records = []
    skipped = 0
    for path in sorted(glob.glob(os.path.join(args.input_dir, "*.json"))):
        try:
            r = json.load(open(path, encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if not isinstance(r, dict) or r.get("request_kind", "inference") != "inference":
            skipped += 1
            continue
        request = r.get("request") or {}
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            skipped += 1
            continue
        ah = request.get("agent_hint") or {}
        sess = (ah.get("session") or {}) if isinstance(ah, dict) else {}
        sid = sess.get("session_id") or r.get("session_id")
        parent = sess.get("parent_session_id") or r.get("parent_session_id") or sid
        if not sid:
            skipped += 1
            continue
        hint = json.loads(json.dumps(ah)) if isinstance(ah, dict) and ah else {}
        hs = hint.setdefault("session", {})
        hs["session_id"] = sid
        hs["parent_session_id"] = parent
        hint.setdefault("agent_status", {"state": "running"})
        usage = r.get("usage") or {}
        completion = int(usage.get("completion_tokens") or 0)
        default_out = args.default_max_out_len or 256
        cap = args.completion_token_cap or 256
        max_out = completion if completion > 0 else default_out
        max_out = max(1, min(int(max_out), cap))
        timing = r.get("timing") or {}
        ts = ((timing.get("source_utc") or {}).get("start_time")
              or r.get("start_time") or 0)
        try:
            ts = int(ts)
        except Exception:
            ts = 0
        row = {
            "question": r.get("case_id") or "",
            "max_out_len": max_out,
            "timestamp": ts,
            "messages": json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
            "agent_hint": hint,
        }
        tools = request.get("tools")
        if isinstance(tools, list) and tools:
            row["tools"] = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        for key in ("tool_choice", "temperature", "top_p", "seed", "enable_thinking"):
            if key in request:
                row[key] = request[key]
        records.append({"ordinal": r.get("global_request_ordinal") or r.get("ordinal") or 0,
                        "timestamp": ts, "sid": sid, "row": row})
    records.sort(key=lambda x: (x["timestamp"], x["ordinal"]))

    # optional finalize derivation
    if args.state_mode == "completed-finalize":
        last = {}
        for item in records:
            last[item["sid"]] = item
        for item in last.values():
            item["row"]["agent_hint"]["agent_status"] = {"state": "finalize"}

    sessions = {item["sid"] for item in records}
    coverage = {"priority": 0, "cache_control": 0, "prediction": 0, "agent_status": 0, "agent_type": 0}
    lens = []
    for item in records:
        h = item["row"]["agent_hint"]
        if h.get("priority"):
            coverage["priority"] += 1
        if h.get("cache_control"):
            coverage["cache_control"] += 1
        if h.get("prediction"):
            coverage["prediction"] += 1
        if h.get("agent_status"):
            coverage["agent_status"] += 1
        if (h.get("session") or {}).get("agent_type"):
            coverage["agent_type"] += 1
        lens.append(header_len(h))
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as out:
        for item in records:
            out.write(json.dumps(item["row"], ensure_ascii=False) + "\n")
    report = {
        "input_dir": args.input_dir,
        "output": args.output,
        "input_records": len(glob.glob(os.path.join(args.input_dir, "*.json"))),
        "output_records": len(records),
        "skipped": skipped,
        "sessions": len(sessions),
        "hint_field_coverage": coverage,
        "header_len_max": max(lens) if lens else 0,
        "header_len_limit": 16384,
        "max_out_len": {
            "min": min((r["row"]["max_out_len"] for r in records), default=0),
            "median": statistics.median([r["row"]["max_out_len"] for r in records]) if records else 0,
            "max": max((r["row"]["max_out_len"] for r in records), default=0),
        },
        "state_mode": args.state_mode,
    }
    report_path = args.report or (args.output + ".report.json")
    with open(report_path, "w", encoding="utf-8") as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="model_io (harness capture) -> Agent Hint v2 JSONL")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--report", default=None)
    ap.add_argument("--default-max-out-len", type=int, default=256)
    ap.add_argument("--completion-token-cap", type=int, default=256)
    ap.add_argument("--state-mode", choices=["running", "completed-finalize"], default="running")
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
