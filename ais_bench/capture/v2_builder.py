#!/usr/bin/env python3
"""Build an Agent Hint v2 dataset from harness-capture model_io files.

Unlike the Jiuwen converter, this one PRESERVES the captured AgentHint fields
(session/agent_type/call_index/epoch, priority, cache_control, prediction,
agent_status). It only fills session_id/parent_session_id and a conservative
running state when the capture omitted them.

For real-task captures this builder also:

- consumes the out-of-band ``status.jsonl`` sink so ``finalize`` comes from a
  real ``turn/end(completed)`` event instead of a per-session guess;
- validates ``prompt(messages + tools + chat template) + max_out_len`` against
  the target context and trims/drops overflowing rows;
- keeps every optional row column on every row (null when absent) so older
  ``datasets.Dataset.from_list`` versions do not infer the Arrow schema from
  the first row and silently drop later columns.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

EXCLUDED_REQUEST_FIELDS = {
    "messages",
    "model",
    "stream",
    "stream_options",
    "agent_hint",
    "max_tokens",
    "max_completion_tokens",
}
REQUIRED_ROW_FIELDS = {"question", "max_out_len", "timestamp", "messages", "agent_hint"}


def header_len(hint) -> int:
    raw = json.dumps(hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def _stat(values):
    if not values:
        return {"min": 0, "median": 0, "max": 0}
    values = sorted(values)
    return {
        "min": values[0],
        "median": statistics.median(values),
        "max": values[-1],
    }


def _load_status_events(path):
    """Return (state_counts, finalized_sessions, finalize_event_by_session)."""
    counts = Counter()
    finalized = set()
    events = {}
    if not path or not os.path.exists(path):
        return counts, finalized, events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            status = event.get("agent_status") or {}
            state = status.get("state")
            sid = (event.get("session") or {}).get("session_id")
            if state:
                counts[str(state)] += 1
            if state == "finalize" and sid:
                finalized.add(str(sid))
                events[str(sid)] = event
    return counts, finalized, events


def _load_all_status_events(path):
    """Return {session_id: [event, ...]} sorted by state_since_ms.

    Used by ``--replay-pause-events`` to turn real harness pause/tool events
    into AISBench pause/resume rows.
    """
    events_by_sid = defaultdict(list)
    if not path or not os.path.exists(path):
        return events_by_sid
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            status = event.get("agent_status") or {}
            sid = (event.get("session") or {}).get("session_id")
            state = status.get("state")
            event_time = status.get("state_since_ms")
            if not sid or not state or event_time is None:
                continue
            try:
                event_time = int(event_time)
            except (TypeError, ValueError):
                continue
            events_by_sid[str(sid)].append({
                "time": event_time,
                "state": str(state).lower(),
                "event": event,
            })
    for sid in events_by_sid:
        events_by_sid[sid].sort(key=lambda item: item["time"])
    return events_by_sid


def _normalize_cache_control(hint) -> bool:
    """Motor replay accepts a single cache_control dict, not the list form.

    DSH emits a list; keep the raw capture untouched and normalize only the
    replay dataset. Returns True when a list was converted/dropped.
    """
    if not isinstance(hint, dict):
        return False
    value = hint.get("cache_control")
    if not isinstance(value, list):
        return False
    first = value[0] if value and isinstance(value[0], dict) else None
    if first is None:
        hint.pop("cache_control", None)
    else:
        hint["cache_control"] = first
    return True


def _set_state(row, state):
    hint = row.get("agent_hint")
    if not isinstance(hint, dict):
        hint = {}
        row["agent_hint"] = hint
    status = dict(hint.get("agent_status") or {})
    status["state"] = state
    hint["agent_status"] = status


def _replay_pause_events(records, events_by_sid, args):
    """Map real pause/tool status events to pause/resume rows.

    For a pause event at ``t`` we pause the last request that started before
    ``t`` and mark the next request with ``resume_before=True``; the next
    request is still allowed to be a pause row itself, so consecutive tool
    waits are representable.
    """
    stats = {
        "enabled": True,
        "applied": 0,
        "skipped": 0,
        "skip_reasons": Counter(),
        "delay_ms": [],
        "resume_before_rows": 0,
    }
    min_delay = max(0, int(getattr(args, "pause_delay_min_ms", 0) or 0))
    by_sid = defaultdict(list)
    for index, item in enumerate(records):
        by_sid[item["sid"]].append(index)

    applied_prev = set()
    for sid, indexes in by_sid.items():
        indexes.sort(key=lambda idx: (records[idx]["timestamp"], records[idx]["ordinal"]))
        events = events_by_sid.get(sid, [])
        if not events:
            continue
        for event in events:
            if event["state"] not in ("pause", "suspend", "tool"):
                continue
            event_time = event["time"]
            previous_local = None
            for local, record_index in enumerate(indexes):
                if records[record_index]["timestamp"] <= event_time:
                    previous_local = local
                else:
                    break
            if previous_local is None:
                stats["skipped"] += 1
                stats["skip_reasons"]["no_previous_request"] += 1
                continue
            if previous_local + 1 >= len(indexes):
                stats["skipped"] += 1
                stats["skip_reasons"]["no_next_request"] += 1
                continue
            prev_index = indexes[previous_local]
            next_index = indexes[previous_local + 1]
            prev_row = records[prev_index]["row"]
            next_row = records[next_index]["row"]
            prev_state = str(((prev_row.get("agent_hint") or {}).get("agent_status") or {}).get("state", "")).lower()
            next_state = str(((next_row.get("agent_hint") or {}).get("agent_status") or {}).get("state", "")).lower()
            if prev_state == "finalize" or next_state == "finalize":
                stats["skipped"] += 1
                stats["skip_reasons"]["finalize_boundary"] += 1
                continue
            if prev_index in applied_prev:
                stats["skipped"] += 1
                stats["skip_reasons"]["previous_row_already_paused"] += 1
                continue
            if next_row.get("resume_before"):
                stats["skipped"] += 1
                stats["skip_reasons"]["next_row_already_resumed"] += 1
                continue
            delay_ms = max(0, int(records[next_index]["timestamp"]) - event_time)
            if min_delay > 0:
                delay_ms = max(delay_ms, min_delay)
            _set_state(prev_row, "pause")
            prev_row["delay_after_ms"] = delay_ms
            next_row["resume_before"] = True
            applied_prev.add(prev_index)
            stats["applied"] += 1
            stats["delay_ms"].append(delay_ms)
            stats["resume_before_rows"] += 1
    stats["skip_reasons"] = dict(stats["skip_reasons"])
    stats["delay_ms"] = _stat(stats["delay_ms"])
    return stats


def _load_tokenizer(path):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "transformers is required for --tokenizer validation; install it "
            "or pass --skip-tokenizer-check explicitly"
        ) from exc
    return AutoTokenizer.from_pretrained(path, trust_remote_code=True)


def _decode_json_field(value):
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return json.loads(value)
    return value


def count_prompt_tokens(tokenizer, row):
    messages = _decode_json_field(row.get("messages"))
    if not isinstance(messages, list) or not messages:
        raise ValueError("missing or invalid messages")
    tools = _decode_json_field(row.get("tools"))
    kwargs = {"tokenize": False, "add_generation_prompt": True}
    if tools:
        kwargs["tools"] = tools
    if row.get("enable_thinking") is not None:
        kwargs["enable_thinking"] = row["enable_thinking"]
    try:
        text = tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        text = tokenizer.apply_chat_template(messages, **kwargs)
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def _set_finalize(row, event):
    hint = row.get("agent_hint")
    if not isinstance(hint, dict):
        hint = {}
        row["agent_hint"] = hint
    status = dict(hint.get("agent_status") or {})
    status["state"] = "finalize"
    event_status = (event or {}).get("agent_status") or {}
    if event_status.get("state_since_ms") is not None:
        status["state_since_ms"] = event_status["state_since_ms"]
    hint["agent_status"] = status


def build(args) -> int:
    records = []
    skipped = 0
    cache_control_list_rows = 0
    cache_control_normalized = 0
    input_paths = sorted(
        p for p in glob.glob(os.path.join(args.input_dir, "*.json"))
        if os.path.basename(p) != "capture.report.json"
    )
    for path in input_paths:
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
        if isinstance(hint.get("cache_control"), list):
            cache_control_list_rows += 1
            if _normalize_cache_control(hint):
                cache_control_normalized += 1
        hs = hint.setdefault("session", {})
        hs["session_id"] = sid
        hs["parent_session_id"] = parent
        hint.setdefault("agent_status", {"state": "running"})
        usage = r.get("usage") or {}
        completion = int(usage.get("completion_tokens") or 0)
        default_out = args.default_max_out_len or 256
        cap = args.completion_token_cap or 0
        max_out = completion if completion > 0 else default_out
        max_out = max(1, int(max_out))
        if cap > 0:
            max_out = min(max_out, int(cap))
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
        elif isinstance(tools, str) and tools.strip():
            row["tools"] = tools
        for key, value in request.items():
            if key in EXCLUDED_REQUEST_FIELDS or key == "tools":
                continue
            row[key] = value
        records.append({
            "ordinal": r.get("global_request_ordinal") or r.get("ordinal") or 0,
            "timestamp": ts,
            "sid": sid,
            "row": row,
        })
    records.sort(key=lambda x: (x["timestamp"], x["ordinal"]))

    # ---- context validation -------------------------------------------------
    context_info = {"dropped": [], "trimmed": [], "tokenizer_error": []}
    tokenizer_enabled = bool(getattr(args, "tokenizer", None)) and not getattr(
        args, "skip_tokenizer_check", False
    )
    if tokenizer_enabled:
        tokenizer = _load_tokenizer(args.tokenizer)
        max_context = int(getattr(args, "max_context", 0) or 0)
        kept = []
        for item in records:
            row = item["row"]
            try:
                prompt_tokens = count_prompt_tokens(tokenizer, row)
            except Exception as exc:
                context_info["tokenizer_error"].append(
                    {"sid": item["sid"], "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            item["prompt_tokens"] = prompt_tokens
            max_out = int(row["max_out_len"])
            if max_context > 0 and prompt_tokens >= max_context:
                context_info["dropped"].append({
                    "sid": item["sid"],
                    "reason": "prompt_exceeds_context",
                    "prompt_tokens": prompt_tokens,
                    "max_out_len": max_out,
                })
                continue
            if max_context > 0 and prompt_tokens + max_out > max_context:
                new_max = max_context - prompt_tokens
                if new_max < 1:
                    context_info["dropped"].append({
                        "sid": item["sid"],
                        "reason": "no_output_budget",
                        "prompt_tokens": prompt_tokens,
                        "max_out_len": max_out,
                    })
                    continue
                context_info["trimmed"].append({
                    "sid": item["sid"],
                    "prompt_tokens": prompt_tokens,
                    "old_max_out_len": max_out,
                    "new_max_out_len": new_max,
                })
                row["max_out_len"] = new_max
            kept.append(item)
        records = kept

    # ---- finalize from real status events ----------------------------------
    status_counts, finalized, finalize_events = _load_status_events(
        getattr(args, "status_events", None)
    )
    last_by_sid = {}
    for item in records:
        last_by_sid[item["sid"]] = item
    incomplete = []
    finalize_source = "none"
    if args.state_mode == "completed-finalize":
        if getattr(args, "status_events", None):
            finalize_source = "status_events"
            for sid, item in last_by_sid.items():
                if sid in finalized:
                    _set_finalize(item["row"], finalize_events.get(sid))
                else:
                    incomplete.append(sid)
        else:
            finalize_source = "derived_last_record"
            for sid, item in last_by_sid.items():
                _set_finalize(item["row"], None)

    # ---- replay real pause/tool events as AISBench pause rows --------------
    pause_replay = {"enabled": False}
    if getattr(args, "replay_pause_events", False):
        status_path = getattr(args, "status_events", None)
        if not status_path:
            pause_replay = {"enabled": False, "reason": "missing --status-events"}
        else:
            events_by_sid = _load_all_status_events(status_path)
            pause_replay = _replay_pause_events(records, events_by_sid, args)

    # ---- keep optional columns on every row --------------------------------
    all_keys = set()
    for item in records:
        all_keys.update(item["row"].keys())
    optional_columns = sorted(all_keys - REQUIRED_ROW_FIELDS)
    for item in records:
        for key in optional_columns:
            item["row"].setdefault(key, None)

    sessions = {item["sid"] for item in records}
    coverage = {
        "priority": 0,
        "cache_control": 0,
        "prediction": 0,
        "agent_status": 0,
        "agent_type": 0,
    }
    body_coverage = Counter()
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
        for key in item["row"]:
            if key not in REQUIRED_ROW_FIELDS:
                body_coverage[key] += 1
        lens.append(header_len(h))

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as out:
        for item in records:
            out.write(json.dumps(item["row"], ensure_ascii=False) + "\n")

    report = {
        "input_dir": args.input_dir,
        "output": args.output,
        "input_records": len(input_paths),
        "output_records": len(records),
        "skipped": skipped,
        "sessions": len(sessions),
        "hint_field_coverage": coverage,
        "cache_control_original_list_rows": cache_control_list_rows,
        "cache_control_normalized": cache_control_normalized,
        "body_field_coverage": dict(body_coverage),
        "optional_columns": optional_columns,
        "header_len_max": max(lens) if lens else 0,
        "header_len_limit": 16384,
        "max_out_len": _stat([r["row"]["max_out_len"] for r in records]),
        "state_mode": args.state_mode,
        "finalize_source": finalize_source,
        "finalized_sessions": len(finalized & sessions),
        "incomplete_sessions": len(incomplete),
        "incomplete_session_ids": incomplete,
        "status_event_counts": dict(status_counts),
        "tokenizer": getattr(args, "tokenizer", None) if tokenizer_enabled else None,
        "tokenizer_check": tokenizer_enabled,
        "max_context": getattr(args, "max_context", None),
        "prompt_tokens": _stat([item["prompt_tokens"] for item in records
                                if "prompt_tokens" in item]),
        "context_dropped": len(context_info["dropped"]),
        "context_trimmed": len(context_info["trimmed"]),
        "context": context_info,
        "pause_event_replay": pause_replay,
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
    ap.add_argument("--status-events", default=None,
                    help="status.jsonl sink; when set, finalize comes from real events.")
    ap.add_argument("--replay-pause-events", action="store_true",
                    help="turn real pause/tool status events into pause/resume rows.")
    ap.add_argument("--pause-delay-min-ms", type=int, default=0,
                    help="floor for delay_after_ms written on replayed pause rows.")
    ap.add_argument("--tokenizer", default=None)
    ap.add_argument("--max-context", type=int, default=None)
    ap.add_argument("--skip-tokenizer-check", action="store_true")
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
