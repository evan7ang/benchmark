#!/usr/bin/env python3
"""Validate an Agent Hint v2 dataset produced by the capture pipeline."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict


def validate(path: str, hints: str, require_finalize: bool = False) -> int:
    errors, warnings = [], []
    rows = 0
    finalize = 0
    sessions = defaultdict(list)
    coverage = {"priority": 0, "cache_control": 0, "prediction": 0,
                "agent_status": 0, "agent_type": 0, "call_index": 0}
    max_out = []
    for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        rows += 1
        try:
            r = json.loads(line)
        except Exception as e:
            errors.append(f"line {lineno}: invalid json: {e}")
            continue
        for key, typ in (("question", str), ("max_out_len", int), ("timestamp", int),
                         ("messages", str), ("agent_hint", dict)):
            if key not in r:
                errors.append(f"line {lineno}: missing {key}")
            elif not isinstance(r[key], typ):
                errors.append(f"line {lineno}: {key} has type {type(r[key]).__name__}")
        try:
            msgs = json.loads(r.get("messages") or "[]")
            if not isinstance(msgs, list) or not msgs:
                errors.append(f"line {lineno}: messages is not a non-empty list")
        except Exception as e:
            errors.append(f"line {lineno}: messages not JSON: {e}")
        if "tools" in r:
            try:
                tools = json.loads(r["tools"])
                if not isinstance(tools, list):
                    errors.append(f"line {lineno}: tools is not a list")
            except Exception as e:
                errors.append(f"line {lineno}: tools not JSON: {e}")
        hint = r.get("agent_hint") or {}
        sess = (hint.get("session") or {}) if isinstance(hint, dict) else {}
        sid = sess.get("session_id")
        if not sid:
            errors.append(f"line {lineno}: missing agent_hint.session.session_id")
            continue
        sessions[sid].append(r)
        if str((hint.get("agent_status") or {}).get("state", "")).lower() == "finalize":
            finalize += 1
        for k in coverage:
            if hint.get(k) or (k == "agent_type" and sess.get("agent_type")) or (k == "call_index" and sess.get("call_index") is not None):
                coverage[k] += 1
        max_out.append(int(r.get("max_out_len") or 0))
    if hints == "on":
        if coverage["agent_status"] == 0:
            warnings.append("hints=on but no agent_status in any record")
        if coverage["priority"] == 0 and coverage["cache_control"] == 0 and coverage["prediction"] == 0:
            warnings.append("hints=on but no priority/cache_control/prediction captured")
    else:
        if coverage["priority"] or coverage["cache_control"] or coverage["prediction"]:
            warnings.append("hints=off but priority/cache_control/prediction present")
    for sid, items in sessions.items():
        idx = [((((x.get("agent_hint") or {}).get("session") or {}).get("call_index")))
               for x in items]
        idx = [i for i in idx if isinstance(i, int)]
        if idx and any(b < a for a, b in zip(idx, idx[1:])):
            warnings.append(f"session {sid}: call_index not monotonic")
    if require_finalize and finalize == 0:
        errors.append("require_finalize=True but no agent_status.state=finalize record")
    report = {
        "dataset": path,
        "hints": hints,
        "rows": rows,
        "sessions": len(sessions),
        "finalize_requests": finalize,
        "hint_field_coverage": coverage,
        "max_out_len": {"min": min(max_out) if max_out else 0,
                        "median": statistics.median(max_out) if max_out else 0,
                        "max": max(max_out) if max_out else 0},
        "errors": errors[:50],
        "warnings": warnings[:50],
        "ok": not errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="validate a captured Agent Hint v2 dataset")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--hints", choices=["on", "off"], default="on")
    ap.add_argument("--require-finalize", action="store_true")
    args = ap.parse_args()
    return validate(args.dataset, args.hints, args.require_finalize)


if __name__ == "__main__":
    sys.exit(main())
