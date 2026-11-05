#!/usr/bin/env python
"""Build an AISBench Agent Hint v2 dataset from JiuwenSwarm model_io captures.

The capture is the legacy body-hint trace produced by JiuwenSwarm
(``request.agent_hint`` with flat session_id / parent_session_id).
This converter only keeps fields that are actually present in the capture:

- session.session_id / parent_session_id: copied from request.agent_hint
- agent_status.state: "running" for every inference request (the capture has
  no lifecycle events). Pass ``--state-mode completed-finalize`` to additionally
  mark the last request of a session as "finalize" when the capture proves the
  session ended normally; that mode is a documented derivation, not captured
  data.
- priority / cache_control / epoch / call_index: not present in the capture,
  therefore NOT synthesized.

Management requests (legacy context_management evict) are dropped because the
v2 Header protocol has no manage_request equivalent.

messages/tools are stored as JSON strings to avoid Arrow struct padding and are
parsed back by VLLMCustomAPI when ``json_extra_fields=["messages","tools"]``.
"""

import argparse
import base64
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert JiuwenSwarm model_io captures to an Agent Hint v2 JSONL dataset."
    )
    parser.add_argument("--input-dir", required=True,
                        help="Directory containing model_io *.json captures.")
    parser.add_argument("--output", required=True,
                        help="Output AISBench JSONL path.")
    parser.add_argument("--report", default=None,
                        help="Report JSON path. Defaults to <output>.report.json.")
    parser.add_argument("--session-limit", type=int, default=None,
                        help="Keep only the first N sessions in timestamp order.")
    parser.add_argument("--max-records", type=int, default=None,
                        help="Hard cap on output records after session filtering.")
    parser.add_argument("--completion-token-cap", type=int, default=8192,
                        help="Upper bound for max_out_len (default: 8192).")
    parser.add_argument("--default-max-out-len", type=int, default=1024,
                        help="Fallback max_out_len when usage.completion_tokens is missing/0.")
    parser.add_argument("--max-context", type=int, default=32768,
                        help="Engine max context used to cap max_out_len (default: 32768).")
    parser.add_argument("--context-reserve", type=int, default=1024,
                        help="Reserved context tokens when capping max_out_len (default: 1024).")
    parser.add_argument("--state-mode", choices=["running", "completed-finalize"],
                        default="running",
                        help="running: no lifecycle derivation (default). "
                             "completed-finalize: derive finalize for sessions whose last "
                             "capture record is a completed inference request.")
    parser.add_argument("--exclude-compressor", action="store_true",
                        help="Drop internal ':compressor:' sub-sessions.")
    return parser.parse_args()


def hint_of(record):
    return (record.get("request") or {}).get("agent_hint") or {}


def session_id_of(record):
    hint = hint_of(record)
    return hint.get("session_id") or record.get("session_id") or ""


def parent_session_id_of(record):
    hint = hint_of(record)
    return hint.get("parent_session_id") or record.get("parent_session_id") or ""


def ordinal_of(record):
    return record.get("global_request_ordinal") or record.get("ordinal") or 0


def timestamp_ms_of(record):
    timing = record.get("timing") or {}
    value = (
        (timing.get("source_utc") or {}).get("start_time")
        or record.get("start_time")
        or timing.get("start_time")
    )
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e12 else value * 1000)
    text = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def encoded_header_len(hint):
    raw = json.dumps(
        hint, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def load_records(input_dir):
    records = []
    for path in sorted(glob.glob(os.path.join(input_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except Exception as exc:
            print(f"[warn] skip {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(record, dict):
            continue
        record["_source_path"] = path
        records.append(record)
    return records


def build_dataset(args, records):
    stats = Counter()
    all_by_sid = defaultdict(list)
    for record in records:
        sid = session_id_of(record)
        if sid:
            all_by_sid[sid].append(record)
    for items in all_by_sid.values():
        items.sort(key=lambda item: (ordinal_of(item), timestamp_ms_of(item) or 0))

    all_inference_by_sid = defaultdict(list)
    for record in records:
        if record.get("request_kind") != "inference":
            continue
        sid = session_id_of(record)
        if sid:
            all_inference_by_sid[sid].append(record)
    for items in all_inference_by_sid.values():
        items.sort(key=lambda item: (ordinal_of(item), timestamp_ms_of(item) or 0))
    true_last_inference = {
        sid: items[-1] for sid, items in all_inference_by_sid.items()
    }

    candidates = []
    for record in records:
        if record.get("request_kind") != "inference":
            stats["management_skipped"] += 1
            continue
        hint = hint_of(record)
        sid = hint.get("session_id") or record.get("session_id") or ""
        if not sid:
            stats["missing_session_id"] += 1
            continue
        if args.exclude_compressor and ":compressor:" in sid:
            stats["compressor_skipped"] += 1
            continue
        if ":compressor:" in sid:
            stats["compressor_included"] += 1
        request = record.get("request") or {}
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            stats["missing_messages"] += 1
            continue
        ts_ms = timestamp_ms_of(record)
        if ts_ms is None:
            stats["missing_timestamp"] += 1
            continue
        usage = record.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        if input_tokens is None:
            input_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
        available = args.max_context - int(input_tokens) - args.context_reserve
        if available <= 0:
            stats["context_skipped"] += 1
            continue
        cap = max(1, min(args.completion_token_cap, available))
        if completion_tokens > 0:
            max_out_len = max(1, min(int(completion_tokens), cap))
        else:
            max_out_len = max(1, min(args.default_max_out_len, cap))

        parent_sid = hint.get("parent_session_id") or record.get("parent_session_id") or sid
        agent_hint = {
            "session": {
                "session_id": sid,
                "parent_session_id": parent_sid,
            },
            "agent_status": {"state": "running"},
        }
        row = {
            "question": record.get("case_id") or "",
            "max_out_len": max_out_len,
            "timestamp": ts_ms,
            "messages": json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
            "agent_hint": agent_hint,
        }
        tools = request.get("tools")
        if isinstance(tools, list) and tools:
            row["tools"] = json.dumps(
                tools, ensure_ascii=False, separators=(",", ":")
            )
        for key in ("tool_choice", "temperature", "top_p", "seed", "enable_thinking"):
            value = request.get(key)
            if value is not None:
                row[key] = value
        candidates.append({
            "row": row,
            "sid": sid,
            "ts_ms": ts_ms,
            "ordinal": ordinal_of(record),
            "record": record,
        })

    candidates.sort(key=lambda item: (item["ts_ms"], item["ordinal"]))

    if args.session_limit is not None:
        keep_sids = []
        for item in candidates:
            if item["sid"] not in keep_sids:
                keep_sids.append(item["sid"])
            if len(keep_sids) > args.session_limit:
                break
        keep_sids = set(keep_sids[:args.session_limit])
        candidates = [item for item in candidates if item["sid"] in keep_sids]

    if args.state_mode == "completed-finalize":
        for item in candidates:
            record = item["record"]
            if true_last_inference.get(item["sid"]) is not record:
                continue
            if record.get("trace_status") == "completed" and \
                    record.get("terminal_reason") == "completed":
                item["row"]["agent_hint"]["agent_status"]["state"] = "finalize"
                stats["derived_finalize"] += 1
    else:
        stats["derived_finalize"] = 0

    if args.max_records is not None:
        candidates = candidates[:args.max_records]

    state_counts = Counter(
        item["row"]["agent_hint"]["agent_status"]["state"] for item in candidates
    )
    session_ids = {item["sid"] for item in candidates}
    header_lens = [
        encoded_header_len(item["row"]["agent_hint"]) for item in candidates
    ]
    max_out_lens = sorted(item["row"]["max_out_len"] for item in candidates)
    timestamps = [item["ts_ms"] for item in candidates]

    report = {
        "input_dir": os.path.abspath(args.input_dir),
        "output": os.path.abspath(args.output),
        "input_records": len(records),
        "output_records": len(candidates),
        "sessions": len(session_ids),
        "state_counts": dict(state_counts),
        "derived_finalize": stats["derived_finalize"],
        "management_skipped": stats["management_skipped"],
        "compressor_included": stats["compressor_included"],
        "compressor_skipped": stats["compressor_skipped"],
        "missing_session_id": stats["missing_session_id"],
        "missing_messages": stats["missing_messages"],
        "missing_timestamp": stats["missing_timestamp"],
        "context_skipped": stats["context_skipped"],
        "max_out_len": {
            "min": max_out_lens[0] if max_out_lens else None,
            "median": max_out_lens[len(max_out_lens) // 2] if max_out_lens else None,
            "max": max_out_lens[-1] if max_out_lens else None,
        },
        "header_len_max": max(header_lens) if header_lens else None,
        "header_len_limit": 16384,
        "timestamp_first_ms": timestamps[0] if timestamps else None,
        "timestamp_last_ms": timestamps[-1] if timestamps else None,
        "state_mode": args.state_mode,
        "notes": [
            "session_id / parent_session_id are copied from request.agent_hint.",
            "agent_status.state=running is the only captured-compatible state.",
            "priority/cache_control/epoch/call_index are not in the capture and are not synthesized.",
            "management/evict requests are dropped (no v2 Header equivalent).",
            "completed-finalize mode derives finalize from the last completed "
            "inference request; it is a documented derivation, not captured data.",
        ],
    }
    return [item["row"] for item in candidates], report


def main():
    args = parse_args()
    records = load_records(args.input_dir)
    rows, report = build_dataset(args, records)
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    report_path = args.report or (output_path + ".report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
