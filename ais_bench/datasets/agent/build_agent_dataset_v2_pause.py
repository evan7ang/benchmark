#!/usr/bin/env python
"""Post-process an Agent Hint v2 finalize JSONL into a pause/offload dataset.

The input is the audited v2 dataset produced by
``build_agent_dataset_v2_from_model_io.py``.  This script does NOT add or
remove inference requests: it rewrites the ``agent_status.state`` of existing
rows so that selected sessions perform:

    running -> ... -> pause -> resume -> ... -> finalize

Corrections applied relative to the first version of the requirement:

- A pause is placed on the Nth request of a session (1-based).  The next
  request becomes ``resume``.  The pause candidate must have a following row
  that is not the last row, so the final row is never rewritten to ``resume``.
  Consequently a session needs at least ``pause_every + 2`` requests; a
  session with 3 requests cannot host pause -> resume -> finalize.
- Sessions whose last captured row is not ``finalize`` are preserved as-is and
  reported as unfinished.  ``finalize`` is never synthesized.
- ``cache_control`` / ``priority`` / ``epoch`` are never added.
- Every request is validated against the target tokenizer:
  ``prompt_tokens(messages + tools + chat template) + max_out_len <= max_context``.
  Rows that overflow are trimmed or dropped before pause insertion, so no
  pause/resume pair is orphaned by a dropped row.
- Non-null ``delay_after_ms`` is only written on pause/suspend/tool rows and
  must be greater than ``idle_offload_s * 1000 + 5000``.  Every row keeps the
  column (null elsewhere) because old ``datasets`` versions infer the Arrow
  schema from the first row and would otherwise drop it.
"""

import argparse
import base64
import copy
import json
import os
import random
import statistics
import sys
from collections import Counter, defaultdict


def parse_args():
    parser = argparse.ArgumentParser(
        description="Add v2 pause/resume lifecycle states to a finalize JSONL dataset."
    )
    parser.add_argument("--input", required=True, help="Input v2 finalize JSONL.")
    parser.add_argument("--output", required=True, help="Output pause/offload JSONL.")
    parser.add_argument(
        "--report",
        default=None,
        help="Report JSON path. Defaults to <output>.report.json.",
    )
    parser.add_argument(
        "--pause-every",
        type=int,
        default=3,
        help="Place a pause on every Nth request of a session (1-based, default: 3).",
    )
    parser.add_argument(
        "--min-session-requests",
        type=int,
        default=3,
        help="Do not consider sessions with fewer requests than this (default: 3).",
    )
    parser.add_argument(
        "--idle-offload-s",
        type=int,
        default=30,
        help="Motor kvc_lifecycle_config.idle_offload_s (default: 30).",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=40000,
        help="Base delay_after_ms written on pause rows (default: 40000).",
    )
    parser.add_argument(
        "--jitter-ms",
        type=int,
        default=0,
        help="Non-negative random jitter added to delay_after_ms (default: 0).",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Random seed for jitter (default: 0)."
    )
    parser.add_argument(
        "--tokenizer",
        default="/home/RAG_data/models/Qwen3-8B",
        help="Tokenizer / chat-template source (default: /home/RAG_data/models/Qwen3-8B).",
    )
    parser.add_argument(
        "--max-context",
        type=int,
        default=32768,
        help="Engine max context length (default: 32768).",
    )
    parser.add_argument(
        "--skip-tokenizer-check",
        action="store_true",
        help="Explicitly skip tokenizer validation (NOT recommended; recorded in report).",
    )
    return parser.parse_args()


def load_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: row is not an object")
            rows.append(row)
    return rows


def dump_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def session_id_of(row):
    hint = row.get("agent_hint")
    if isinstance(hint, dict):
        session = hint.get("session")
        if isinstance(session, dict):
            value = session.get("session_id")
            if value is not None:
                return str(value)
        value = hint.get("session_id")
        if value is not None:
            return str(value)
    value = row.get("session_id")
    return str(value) if value is not None else ""


def state_of(row):
    hint = row.get("agent_hint")
    if isinstance(hint, dict):
        status = hint.get("agent_status")
        if isinstance(status, dict) and status.get("state") is not None:
            return str(status.get("state"))
        if hint.get("stage") is not None:
            return str(hint.get("stage"))
    if row.get("stage") is not None:
        return str(row.get("stage"))
    return ""


def set_state(row, state):
    hint = row.get("agent_hint")
    if not isinstance(hint, dict):
        hint = {}
        row["agent_hint"] = hint
    status = hint.get("agent_status")
    if not isinstance(status, dict):
        status = {}
        hint["agent_status"] = status
    status["state"] = state


def encoded_header_len(hint):
    raw = json.dumps(hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def _decode_json_field(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def load_tokenizer(path):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "transformers is required for tokenizer validation; "
            "install it or pass --skip-tokenizer-check explicitly"
        ) from exc
    return AutoTokenizer.from_pretrained(path, trust_remote_code=True)


def count_prompt_tokens(tokenizer, row):
    messages = _decode_json_field(row.get("messages"))
    if not isinstance(messages, list) or not messages:
        raise ValueError("missing or invalid messages")
    tools = _decode_json_field(row.get("tools"))
    kwargs = {"tokenize": False, "add_generation_prompt": True}
    if tools:
        kwargs["tools"] = tools
    text = tokenizer.apply_chat_template(messages, **kwargs)
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def fit_context(rows, tokenizer, max_context, enabled=True):
    """Validate prompt + max_out_len and drop/trim overflowing rows.

    Returns (kept_rows, kept_orig_indices, prompt_tokens_by_id, info).
    """
    kept_rows = []
    kept_orig = []
    prompt_tokens_by_id = {}
    dropped = []
    trimmed = []

    for orig_index, row in enumerate(rows):
        if not enabled:
            kept_rows.append(row)
            kept_orig.append(orig_index)
            continue
        max_out = int(row.get("max_out_len") or 0)
        try:
            prompt_tokens = count_prompt_tokens(tokenizer, row)
        except Exception as exc:  # malformed messages / template failure
            dropped.append(
                {
                    "input_index": orig_index,
                    "session_id": session_id_of(row),
                    "state": state_of(row),
                    "reason": "tokenizer_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        if prompt_tokens >= max_context:
            dropped.append(
                {
                    "input_index": orig_index,
                    "session_id": session_id_of(row),
                    "state": state_of(row),
                    "reason": "prompt_exceeds_context",
                    "prompt_tokens": prompt_tokens,
                    "max_out_len": max_out,
                }
            )
            continue
        if prompt_tokens + max_out > max_context:
            new_max_out = max_context - prompt_tokens
            if new_max_out < 1:
                dropped.append(
                    {
                        "input_index": orig_index,
                        "session_id": session_id_of(row),
                        "state": state_of(row),
                        "reason": "no_output_budget",
                        "prompt_tokens": prompt_tokens,
                        "max_out_len": max_out,
                    }
                )
                continue
            trimmed.append(
                {
                    "input_index": orig_index,
                    "session_id": session_id_of(row),
                    "state": state_of(row),
                    "prompt_tokens": prompt_tokens,
                    "old_max_out_len": max_out,
                    "new_max_out_len": new_max_out,
                }
            )
            row["max_out_len"] = new_max_out

        prompt_tokens_by_id[id(row)] = prompt_tokens
        kept_rows.append(row)
        kept_orig.append(orig_index)

    return kept_rows, kept_orig, prompt_tokens_by_id, {"dropped": dropped, "trimmed": trimmed}


def delay_is_valid(value, idle_offload_s):
    return int(value) > int(idle_offload_s) * 1000 + 5000


def insert_pause_states(rows, args):
    """Rewrite pause/resume states in-place and return statistics."""
    rng = random.Random(args.seed)
    groups = defaultdict(list)
    for pos, row in enumerate(rows):
        sid = session_id_of(row)
        if sid:
            groups[sid].append(pos)

    stats = {
        "pause_points": 0,
        "resume_points": 0,
        "pause_sessions": 0,
        "pause_skipped_sessions": 0,
        "pause_skip_reasons": Counter(),
        "delay_values": [],
        "pre_existing_delay_removed": 0,
        "tool_delay_preserved": 0,
    }

    for row in rows:
        if "delay_after_ms" not in row:
            continue
        state = state_of(row).lower()
        value = row.get("delay_after_ms")
        if value is None:
            continue
        if state in ("pause", "suspend", "tool") and \
                delay_is_valid(value, args.idle_offload_s):
            stats["tool_delay_preserved"] += 1
            stats["delay_values"].append(int(value))
        else:
            row.pop("delay_after_ms", None)
            stats["pre_existing_delay_removed"] += 1

    hard_skip = []
    for _sid, positions in groups.items():
        count = len(positions)
        if count < args.min_session_requests:
            hard_skip.append((sid, "too_few_requests"))
            continue
        candidates = list(range(args.pause_every - 1, count, args.pause_every))
        if not candidates:
            hard_skip.append((sid, "session_too_short_for_pause"))
            continue

        inserted = 0
        reasons = Counter()
        last_resume_local = -10
        for local_i in candidates:
            if local_i + 1 >= count:
                reasons["no_following_row"] += 1
                continue
            if local_i + 1 == count - 1:
                next_state = state_of(rows[positions[local_i + 1]]).lower()
                reasons["next_is_finalize" if next_state == "finalize" else "next_is_last_row"] += 1
                continue
            if local_i <= last_resume_local:
                reasons["would_be_consecutive_pause"] += 1
                continue
            cur_state = state_of(rows[positions[local_i]]).lower()
            next_state = state_of(rows[positions[local_i + 1]]).lower()
            if cur_state != "running":
                reasons[f"current_state_{cur_state or 'empty'}"] += 1
                continue
            if next_state != "running":
                reasons[f"next_state_{next_state or 'empty'}"] += 1
                continue

            jitter = rng.randint(0, max(0, args.jitter_ms))
            delay = args.delay_ms + jitter
            if not delay_is_valid(delay, args.idle_offload_s):
                raise ValueError(
                    f"delay_after_ms={delay} is not greater than "
                    f"idle_offload_s*1000+5000={args.idle_offload_s * 1000 + 5000}"
                )

            set_state(rows[positions[local_i]], "pause")
            rows[positions[local_i]]["delay_after_ms"] = delay
            next_row = rows[positions[local_i + 1]]
            set_state(next_row, "resume")
            next_row.pop("delay_after_ms", None)

            inserted += 1
            stats["pause_points"] += 1
            stats["resume_points"] += 1
            stats["delay_values"].append(delay)
            last_resume_local = local_i + 1

        if inserted:
            stats["pause_sessions"] += 1
        else:
            stats["pause_skipped_sessions"] += 1
            reason = reasons.most_common(1)[0][0] if reasons else "no_valid_candidate"
            stats["pause_skip_reasons"][reason] += 1

    for _sid, reason in hard_skip:
        stats["pause_skipped_sessions"] += 1
        stats["pause_skip_reasons"][reason] += 1

    # Dataset.from_list infers the Arrow schema from the FIRST row in older
    # datasets versions; a column present only on later rows is silently
    # dropped. Keep the control column on every row (null = no delay) so
    # CustomDataset -> GenInferencer can forward it to the session worker.
    for row in rows:
        row.setdefault("delay_after_ms", None)

    return stats


def validate_output(rows, orig_rows, orig_indices, prompt_tokens_by_id, args):
    checks = {}
    problems = []

    by_session = defaultdict(list)
    for pos, row in enumerate(rows):
        by_session[session_id_of(row)].append(pos)

    pause_ok = True
    no_consecutive = True
    last_not_resume = True
    for sid, positions in by_session.items():
        states = [state_of(rows[p]).lower() for p in positions]
        if states and states[-1] == "resume":
            last_not_resume = False
            problems.append(f"session {sid}: last row is resume")
        for idx, state in enumerate(states):
            if state == "pause":
                if idx + 1 >= len(states) or states[idx + 1] != "resume":
                    pause_ok = False
                    problems.append(f"session {sid}: pause at position {idx} not followed by resume")
                if idx + 1 < len(states) and states[idx + 1] == "pause":
                    no_consecutive = False
                    problems.append(f"session {sid}: consecutive pause at {idx}")
    checks["pause_followed_by_resume"] = pause_ok
    checks["no_consecutive_pause"] = no_consecutive
    checks["last_row_not_resume"] = last_not_resume

    finalize_ok = True
    for sid, positions in by_session.items():
        for idx, pos in enumerate(positions):
            if state_of(rows[pos]).lower() == "finalize" and idx != len(positions) - 1:
                finalize_ok = False
                problems.append(f"session {sid}: finalize at non-last row")
    checks["finalize_only_last"] = finalize_ok

    delay_ok = True
    threshold_ok = True
    for row in rows:
        value = row.get("delay_after_ms")
        if value is None:
            continue
        state = state_of(row).lower()
        if state not in ("pause", "suspend", "tool"):
            delay_ok = False
            problems.append(f"delay_after_ms on state={state or 'empty'}")
        elif not delay_is_valid(value, args.idle_offload_s):
            threshold_ok = False
            problems.append(
                f"delay_after_ms={value} not above idle threshold"
            )
    checks["delay_only_on_pause_suspend_tool"] = delay_ok
    checks["delay_above_idle_threshold"] = threshold_ok

    context_ok = True
    for row in rows:
        prompt_tokens = prompt_tokens_by_id.get(id(row))
        if prompt_tokens is None:
            continue
        if prompt_tokens + int(row.get("max_out_len") or 0) > args.max_context:
            context_ok = False
            problems.append(
                f"context overflow: prompt={prompt_tokens} "
                f"max_out={row.get('max_out_len')}"
            )
    checks["context_fits"] = context_ok

    immutable_ok = True
    for row, orig_index in zip(rows, orig_indices):
        original = orig_rows[orig_index]
        for field in ("messages", "tools"):
            if row.get(field) != original.get(field):
                immutable_ok = False
                problems.append(f"input row {orig_index}: field {field} changed")
        if session_id_of(row) != session_id_of(original):
            immutable_ok = False
            problems.append(f"input row {orig_index}: session_id changed")
    checks["messages_tools_session_id_unchanged"] = immutable_ok

    checks["all_passed"] = all(checks.values())
    return checks, problems


def build_report(args, input_rows, output_rows, orig_indices, prompt_tokens_by_id,
                 context_info, pause_stats, checks, problems):
    input_states = Counter(state_of(row) for row in input_rows)
    output_states = Counter(state_of(row) for row in output_rows)

    by_session = defaultdict(list)
    for row in output_rows:
        by_session[session_id_of(row)].append(row)
    unfinished = [
        sid for sid, items in by_session.items()
        if state_of(items[-1]).lower() != "finalize"
    ]

    delay_values = sorted(pause_stats["delay_values"])
    prompt_values = sorted(prompt_tokens_by_id.values())
    header_lens = [
        encoded_header_len(row.get("agent_hint"))
        for row in output_rows
        if isinstance(row.get("agent_hint"), dict)
    ]

    def stat(values):
        if not values:
            return {"min": None, "median": None, "max": None}
        return {
            "min": values[0],
            "median": statistics.median(values),
            "max": values[-1],
        }

    report = {
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.output),
        "tokenizer": None if args.skip_tokenizer_check else os.path.abspath(args.tokenizer),
        "max_context": args.max_context,
        "pause_every": args.pause_every,
        "min_session_requests": args.min_session_requests,
        "idle_offload_s": args.idle_offload_s,
        "delay_after_ms": {
            "base": args.delay_ms,
            "jitter_ms": args.jitter_ms,
            "required_gt": args.idle_offload_s * 1000 + 5000,
            **stat(delay_values),
        },
        "input_rows": len(input_rows),
        "output_rows": len(output_rows),
        "dropped_rows": len(context_info["dropped"]),
        "trimmed_rows": len(context_info["trimmed"]),
        "input_sessions": len({session_id_of(r) for r in input_rows}),
        "output_sessions": len(by_session),
        "state_counts_before": dict(input_states),
        "state_counts_after": dict(output_states),
        "pause_points": pause_stats["pause_points"],
        "resume_points": pause_stats["resume_points"],
        "pause_sessions": pause_stats["pause_sessions"],
        "pause_skipped_sessions": pause_stats["pause_skipped_sessions"],
        "pause_skip_reasons": dict(pause_stats["pause_skip_reasons"]),
        "unfinished_sessions": len(unfinished),
        "unfinished_session_ids": unfinished,
        "pre_existing_delay_removed": pause_stats["pre_existing_delay_removed"],
        "tool_delay_preserved": pause_stats["tool_delay_preserved"],
        "prompt_tokens": stat(prompt_values),
        "header_len_max": max(header_lens) if header_lens else None,
        "header_len_limit": 16384,
        "context": context_info,
        "checks": checks,
        "problems": problems,
        "notes": [
            "No request is added or removed; pause/resume are state rewrites on existing rows.",
            "Pause is placed on the Nth request (1-based); the next request of the same "
            "session becomes resume.",
            "pause_followed_by_resume is checked in per-session order; rows from different "
            "sessions may interleave in the global JSONL order.",
            "The final row of a session is never rewritten to resume.",
            "Sessions whose last captured row is not finalize are reported as unfinished; "
            "finalize is not synthesized.",
            "cache_control / priority / epoch are not added.",
            "Non-null delay_after_ms is only written on pause/suspend/tool rows; "
            "all other rows carry delay_after_ms=null so the Arrow schema keeps the column.",
            "ON and OFF arms must use this same dataset; restart Motor/engine between arms "
            "because finalize is terminal in Motor.",
        ],
    }
    if args.skip_tokenizer_check:
        report["notes"].append(
            "TOKENIZER CHECK WAS SKIPPED EXPLICITLY; context validation is not trustworthy."
        )
    return report


def main():
    args = parse_args()
    if args.pause_every < 1:
        raise ValueError("--pause-every must be >= 1")
    if args.jitter_ms < 0:
        raise ValueError("--jitter-ms must be >= 0 (jitter may only add delay)")
    if not delay_is_valid(args.delay_ms, args.idle_offload_s):
        raise ValueError(
            f"--delay-ms={args.delay_ms} must be > "
            f"idle_offload_s*1000+5000={args.idle_offload_s * 1000 + 5000}"
        )

    input_rows = load_jsonl(args.input)
    input_snapshot = copy.deepcopy(input_rows)

    finalize_not_last = []
    positions_by_session = defaultdict(list)
    for pos, row in enumerate(input_rows):
        positions_by_session[session_id_of(row)].append(pos)
    for sid, positions in positions_by_session.items():
        for idx, pos in enumerate(positions):
            if state_of(input_rows[pos]).lower() == "finalize" and idx != len(positions) - 1:
                finalize_not_last.append((sid, pos))
    if finalize_not_last:
        raise ValueError(
            "input violates the v2 lifecycle invariant 'finalize only on the last row': "
            f"{finalize_not_last[:5]}"
        )

    tokenizer = None
    if not args.skip_tokenizer_check:
        tokenizer = load_tokenizer(args.tokenizer)

    rows, orig_indices, prompt_tokens_by_id, context_info = fit_context(
        input_rows,
        tokenizer,
        args.max_context,
        enabled=not args.skip_tokenizer_check,
    )

    pause_stats = insert_pause_states(rows, args)
    checks, problems = validate_output(
        rows, input_snapshot, orig_indices, prompt_tokens_by_id, args
    )
    report = build_report(
        args, input_snapshot, rows, orig_indices, prompt_tokens_by_id,
        context_info, pause_stats, checks, problems,
    )

    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    dump_jsonl(output_path, rows)
    report_path = args.report or (output_path + ".report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not checks["all_passed"]:
        print("[error] validation checks failed; see report.problems", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
