#!/usr/bin/env python3
"""Build the Issue #52113 TTFT reproduction datasets by selecting real
prefix-continuous windows from the v2 finalize capture.

The source capture does not contain 20 sessions x 5-10 turns with strict
prefix continuity.  Instead of silently relaxing the experiment, this script
enumerates contiguous windows per session, scores them by prefix continuity
and token growth, and selects up to N windows.  Every deviation (shorter
sessions, relaxed prefix threshold) is recorded in the report.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import statistics
from collections import Counter, OrderedDict
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--tokenizer", default="/home/RAG_data/models/Qwen3-8B")
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--safety-margin", type=int, default=512)
    p.add_argument("--max-out", type=int, default=512)
    p.add_argument("--min-max-out", type=int, default=32)
    p.add_argument("--ttl-s", type=int, default=600)
    p.add_argument("--target-sessions", type=int, default=20)
    p.add_argument("--smoke-sessions", type=int, default=3)
    p.add_argument("--min-turns", type=int, default=3)
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--min-prompt", type=int, default=10000)
    p.add_argument("--max-prompt", type=int, default=30000)
    p.add_argument("--min-prefix-ratio", type=float, default=0.8)
    p.add_argument("--min-token-span", type=int, default=1000)
    p.add_argument("--min-msg-growth", type=int, default=2)
    p.add_argument("--min-diverse-tools", type=int, default=2)
    p.add_argument("--skip-tokenizer", action="store_true")
    return p.parse_args()


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return json.loads(value) if value else None
    return value


def session_id_of(row: dict[str, Any]) -> str:
    session = (row.get("agent_hint") or {}).get("session") or {}
    if isinstance(session, dict) and session.get("session_id") is not None:
        return str(session["session_id"])
    value = (row.get("agent_hint") or {}).get("session_id")
    return str(value) if value is not None else ""


def base_root_of(sid: str) -> str:
    return re.split(r":compressor:round-level|_sub_general-purpose_", sid)[0]


def tools_hash(row: dict[str, Any]) -> str:
    tools = parse_json_field(row.get("tools"))
    raw = json.dumps(tools, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:8]


def load_rows(path: str) -> list[dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def group_rows(rows: list[dict[str, Any]]) -> "OrderedDict[str, list[tuple[int, dict[str, Any]]]]":
    grouped: "OrderedDict[str, list[tuple[int, dict[str, Any]]]]" = OrderedDict()
    for idx, row in enumerate(rows):
        grouped.setdefault(session_id_of(row), []).append((idx, row))
    return grouped


def load_tokenizer(path: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(path, trust_remote_code=True)


def prompt_tokens(tokenizer, row: dict[str, Any]) -> int:
    messages = parse_json_field(row.get("messages"))
    tools = parse_json_field(row.get("tools"))
    rendered = tokenizer.apply_chat_template(
        messages,
        tools=tools or None,
        tokenize=True,
        add_generation_prompt=True,
    )
    if hasattr(rendered, "keys") and "input_ids" in rendered:
        rendered = rendered["input_ids"]
    return len(rendered)


def prefix_ratio(messages: list[Any]) -> float:
    relations = []
    for prev, cur in zip(messages, messages[1:]):
        if not isinstance(prev, list) or not isinstance(cur, list):
            relations.append(False)
            continue
        n = min(len(prev), len(cur))
        relations.append(prev[:n] == cur[:n])
    return sum(relations) / len(relations) if relations else 0.0


def message_growth(messages: list[Any]) -> int:
    if len(messages) < 2:
        return 0
    return len(messages[-1]) - len(messages[0])


def make_candidates(
    grouped: "OrderedDict[str, list[tuple[int, dict[str, Any]]]]",
    prompt_tokens_by_index: dict[int, int],
    args: argparse.Namespace,
    *,
    min_prefix_ratio: float,
    min_token_span: int,
    min_msg_growth: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for sid, items in grouped.items():
        if not sid:
            continue
        messages = [parse_json_field(row.get("messages")) for _, row in items]
        counts = [prompt_tokens_by_index[idx] for idx, _ in items]
        n = len(items)
        for start in range(n):
            for turns in range(args.min_turns, min(args.max_turns, n - start) + 1):
                window_counts = counts[start : start + turns]
                if not all(args.min_prompt <= c <= args.max_prompt for c in window_counts):
                    continue
                ratio = prefix_ratio(messages[start : start + turns])
                span = max(window_counts) - min(window_counts)
                growth = message_growth(messages[start : start + turns])
                if ratio < min_prefix_ratio or span < min_token_span or growth < min_msg_growth:
                    continue
                candidates.append(
                    {
                        "session_id": sid,
                        "base_root": base_root_of(sid),
                        "start": start,
                        "turns": turns,
                        "prefix_ratio": ratio,
                        "token_span": span,
                        "message_growth": growth,
                        "prompt_counts": window_counts,
                        "prompt_min": min(window_counts),
                        "prompt_max": max(window_counts),
                        "tools_hash": tools_hash(items[start][1]),
                        "first_index": items[start][0],
                        "score": (
                            round(ratio, 6),
                            span,
                            turns,
                            1 if start == 0 else 0,
                            -start,
                        ),
                    }
                )
    return candidates


def select_windows(
    candidates: list[dict[str, Any]],
    target: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    selected: list[dict[str, Any]] = []
    used_sessions: set[str] = set()
    used_bases: set[str] = set()

    ordered = sorted(candidates, key=lambda c: c["score"], reverse=True)

    # Pass 1: one window per base root.
    for cand in ordered:
        if len(selected) >= target:
            break
        if cand["base_root"] in used_bases or cand["session_id"] in used_sessions:
            continue
        selected.append(cand)
        used_sessions.add(cand["session_id"])
        used_bases.add(cand["base_root"])

    # Pass 2: fill with distinct session ids.
    if len(selected) < target:
        notes.append("pass2_distinct_session_fill")
        for cand in ordered:
            if len(selected) >= target:
                break
            if cand["session_id"] in used_sessions:
                continue
            selected.append(cand)
            used_sessions.add(cand["session_id"])

    if len(selected) < target:
        notes.append(f"only_{len(selected)}_quality_windows")
    return selected, notes


def ensure_tools_diversity(
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    dominant_tools: str,
    minimum: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    selected = list(selected)
    if minimum <= 0:
        return selected, notes

    def diverse_count(items: list[dict[str, Any]]) -> int:
        return sum(1 for item in items if item["tools_hash"] != dominant_tools)

    if diverse_count(selected) >= minimum:
        return selected, notes

    used_sessions = {item["session_id"] for item in selected}
    replacements = sorted(
        [
            c
            for c in candidates
            if c["tools_hash"] != dominant_tools and c["session_id"] not in used_sessions
        ],
        key=lambda c: c["score"],
        reverse=True,
    )
    if not replacements:
        notes.append("no_diverse_tools_candidate")
        return selected, notes

    while diverse_count(selected) < minimum and replacements:
        replacement = replacements.pop(0)
        if replacement["session_id"] in {item["session_id"] for item in selected}:
            continue
        same_tools = [i for i, item in enumerate(selected) if item["tools_hash"] == dominant_tools]
        if not same_tools:
            break
        victim_index = min(same_tools, key=lambda i: selected[i]["score"])
        victim = selected[victim_index]
        selected[victim_index] = replacement
        notes.append(
            f"tools_diversity_swap:{victim['session_id']}->{replacement['session_id']}"
        )
    return selected, notes


def encode_header_len(agent_hint: dict[str, Any]) -> int:
    raw = json.dumps(agent_hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def materialize(
    rows: list[dict[str, Any]],
    grouped: "OrderedDict[str, list[tuple[int, dict[str, Any]]]]",
    selected: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    ttl: bool,
    finalize: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    output: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    # Sort by source order so the request order is stable between arms.
    for cand in sorted(selected, key=lambda c: c["first_index"]):
        sid = cand["session_id"]
        items = grouped[sid][cand["start"] : cand["start"] + cand["turns"]]
        materialized_items: list[dict[str, Any]] = []
        for turn, (source_index, source_row) in enumerate(items):
            row = copy.deepcopy(source_row)
            agent_hint = row.get("agent_hint")
            if not isinstance(agent_hint, dict):
                agent_hint = {}
                row["agent_hint"] = agent_hint

            state = "finalize" if finalize and turn == len(items) - 1 else "running"
            agent_hint.pop("context_management", None)
            agent_hint["agent_status"] = {"state": state}
            agent_hint.pop("cache_control", None)
            if ttl:
                agent_hint["cache_control"] = {"type": "ephemeral", "ttl_s": args.ttl_s}

            old_max_out = int(row.get("max_out_len") or args.max_out)
            max_out = min(old_max_out, args.max_out)
            prompt = cand["prompt_counts"][turn]
            if prompt + max_out + args.safety_margin > args.max_context:
                max_out = args.max_context - prompt - args.safety_margin
            if max_out < args.min_max_out:
                problems.append(
                    f"session {sid} turn {turn}: no output budget "
                    f"(prompt={prompt}, max_out={max_out})"
                )
                return [], [], problems
            row["max_out_len"] = max_out

            tools = parse_json_field(row.get("tools"))
            if not tools:
                row["tool_choice"] = None

            materialized_items.append(row)
        output.extend(materialized_items)
        details.append(
            {
                "session_id": sid,
                "base_root": cand["base_root"],
                "start": cand["start"],
                "turns": cand["turns"],
                "prefix_ratio": cand["prefix_ratio"],
                "token_span": cand["token_span"],
                "message_growth": cand["message_growth"],
                "prompt_counts": cand["prompt_counts"],
                "prompt_min": cand["prompt_min"],
                "prompt_max": cand["prompt_max"],
                "tools_hash": cand["tools_hash"],
                "state_seq": [
                    "finalize" if finalize and i == cand["turns"] - 1 else "running"
                    for i in range(cand["turns"])
                ],
            }
        )
    return output, details, problems


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def validate_output(
    output: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    prompt_counts: dict[int, int],
    args: argparse.Namespace,
    *,
    ttl: bool,
    finalize: bool,
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in output:
        grouped.setdefault(session_id_of(row), []).append(row)

    all_in_range = True
    context_ok = True
    states_ok = True
    ttl_ok = True
    header_max = 0
    immutable_ok = True
    finalize_ok = True
    def find_source(row: dict[str, Any]) -> dict[str, Any] | None:
        # Fallback: match by messages/tools/session_id.  The builder keeps rows
        # in source order, so a linear scan over the same session is enough.
        sid = session_id_of(row)
        for source_row in source_rows:
            if session_id_of(source_row) != sid:
                continue
            if source_row.get("messages") == row.get("messages") and source_row.get("tools") == row.get("tools"):
                return source_row
        return None

    for sid, items in grouped.items():
        states = [((row.get("agent_hint") or {}).get("agent_status") or {}).get("state") for row in items]
        if finalize:
            if states[-1] != "finalize":
                finalize_ok = False
                problems.append(f"session {sid}: last state is {states[-1]!r}")
            if any(state == "finalize" for state in states[:-1]):
                finalize_ok = False
                problems.append(f"session {sid}: finalize before last row")
        for row, state in zip(items, states):
            if state not in ("running", "finalize"):
                states_ok = False
                problems.append(f"session {sid}: unexpected state {state!r}")
            hint = row.get("agent_hint") or {}
            if ttl:
                cache = hint.get("cache_control")
                if not isinstance(cache, dict) or cache.get("ttl_s") != args.ttl_s:
                    ttl_ok = False
                    problems.append(f"session {sid}: missing cache_control")
            header_max = max(header_max, encode_header_len(hint))
            messages = parse_json_field(row.get("messages"))
            tools = parse_json_field(row.get("tools"))
            rendered = args._tokenizer.apply_chat_template(
                messages, tools=tools or None, tokenize=True, add_generation_prompt=True
            )
            if hasattr(rendered, "keys") and "input_ids" in rendered:
                rendered = rendered["input_ids"]
            prompt = len(rendered)
            if not (args.min_prompt <= prompt <= args.max_prompt):
                all_in_range = False
                problems.append(f"session {sid}: prompt {prompt} out of range")
            if prompt + int(row.get("max_out_len") or 0) + args.safety_margin > args.max_context:
                context_ok = False
                problems.append(
                    f"session {sid}: context overflow prompt={prompt} max_out={row.get('max_out_len')}"
                )
            source_row = find_source(row)
            if source_row is None:
                immutable_ok = False
                problems.append(f"session {sid}: cannot find source row")
            else:
                if source_row.get("messages") != row.get("messages"):
                    immutable_ok = False
                    problems.append(f"session {sid}: messages changed")
                if source_row.get("tools") != row.get("tools"):
                    immutable_ok = False
                    problems.append(f"session {sid}: tools changed")
                if session_id_of(source_row) != sid:
                    immutable_ok = False
                    problems.append(f"session {sid}: session_id changed")

    checks = {
        "prompts_in_range": all_in_range,
        "context_fits": context_ok,
        "states_valid": states_ok,
        "finalize_only_last": finalize_ok,
        "cache_control_present": ttl_ok if ttl else True,
        "messages_tools_session_unchanged": immutable_ok,
        "header_len_under_limit": header_max < 16384,
        "all_passed": all(
            [all_in_range, context_ok, states_ok, finalize_ok, immutable_ok, header_max < 16384]
            + ([ttl_ok] if ttl else [])
        ),
    }
    return {"checks": checks, "header_len_max": header_max}, problems


def stats(values: list[int]) -> dict[str, Any]:
    values = sorted(values)
    if not values:
        return {"min": None, "median": None, "max": None}
    return {
        "min": values[0],
        "median": statistics.median(values),
        "max": values[-1],
    }


def main() -> None:
    args = parse_args()
    rows = load_rows(args.input)
    grouped = group_rows(rows)
    dominant_tools = Counter(tools_hash(items[0][1]) for items in grouped.values()).most_common(1)[0][0]

    tokenizer = None
    prompt_counts: dict[int, int] = {}
    if not args.skip_tokenizer:
        tokenizer = load_tokenizer(args.tokenizer)
        for idx, row in enumerate(rows):
            prompt_counts[idx] = prompt_tokens(tokenizer, row)
        args._tokenizer = tokenizer
    else:
        args._tokenizer = None

    candidates = make_candidates(
        grouped,
        prompt_counts,
        args,
        min_prefix_ratio=args.min_prefix_ratio,
        min_token_span=args.min_token_span,
        min_msg_growth=args.min_msg_growth,
    )
    selected, selection_notes = select_windows(candidates, args.target_sessions)
    selected, diversity_notes = ensure_tools_diversity(
        selected, candidates, dominant_tools, args.min_diverse_tools
    )
    selection_notes.extend(diversity_notes)

    relaxed = False
    if len(selected) < args.target_sessions:
        relaxed = True
        relaxed_candidates = make_candidates(
            grouped,
            prompt_counts,
            args,
            min_prefix_ratio=0.5,
            min_token_span=500,
            min_msg_growth=1,
        )
        already = {item["session_id"] for item in selected}
        fill, fill_notes = select_windows(
            [c for c in relaxed_candidates if c["session_id"] not in already],
            args.target_sessions - len(selected),
        )
        selection_notes.extend(fill_notes)
        selected.extend(fill)
        selection_notes.append("relaxed_threshold_fill")

    if not selected:
        raise SystemExit("no eligible windows; check tokenizer or thresholds")

    running_rows, running_details, running_problems = materialize(
        rows, grouped, selected, args, ttl=True, finalize=False
    )
    finalize_rows, finalize_details, finalize_problems = materialize(
        rows, grouped, selected, args, ttl=True, finalize=True
    )
    no_ttl_rows, _, no_ttl_problems = materialize(
        rows, grouped, selected, args, ttl=False, finalize=False
    )

    os.makedirs(args.out_dir, exist_ok=True)
    paths = {
        "running_ttl": os.path.join(args.out_dir, "hint_running_ttl.jsonl"),
        "finalize_ttl": os.path.join(args.out_dir, "hint_finalize_ttl.jsonl"),
        "running_no_ttl": os.path.join(args.out_dir, "hint_running_no_ttl.jsonl"),
        "running_ttl_smoke": os.path.join(args.out_dir, "hint_running_ttl_smoke.jsonl"),
    }
    write_jsonl(paths["running_ttl"], running_rows)
    write_jsonl(paths["finalize_ttl"], finalize_rows)
    write_jsonl(paths["running_no_ttl"], no_ttl_rows)

    smoke_sessions = list(
        OrderedDict.fromkeys(session_id_of(row) for row in running_rows)
    )[: args.smoke_sessions]
    smoke_set = set(smoke_sessions)
    smoke_rows = [row for row in running_rows if session_id_of(row) in smoke_set]
    write_jsonl(paths["running_ttl_smoke"], smoke_rows)

    running_checks, running_validation = validate_output(
        running_rows, rows, prompt_counts, args, ttl=True, finalize=False
    )
    finalize_checks, finalize_validation = validate_output(
        finalize_rows, rows, prompt_counts, args, ttl=True, finalize=True
    )
    no_ttl_checks, no_ttl_validation = validate_output(
        no_ttl_rows, rows, prompt_counts, args, ttl=False, finalize=False
    )

    prompt_values = [count for item in running_details for count in item["prompt_counts"]]
    report = {
        "source": os.path.abspath(args.input),
        "out_dir": os.path.abspath(args.out_dir),
        "tokenizer": None if tokenizer is None else os.path.abspath(args.tokenizer),
        "args": vars(args) | {"_tokenizer": None},
        "source_sessions": len(grouped),
        "source_rows": len(rows),
        "dominant_tools_hash": dominant_tools,
        "candidate_windows": len(candidates),
        "selected_sessions": len({item["session_id"] for item in selected}),
        "selected_windows": len(running_details),
        "selected_rows": len(running_rows),
        "selection_notes": selection_notes,
        "relaxed": relaxed,
        "selected": running_details,
        "prompt_tokens": stats(prompt_values),
        "finalize_counts": {
            "sessions": len({item["session_id"] for item in finalize_details}),
            "finalize": sum(1 for row in finalize_rows if ((row.get("agent_hint") or {}).get("agent_status") or {}).get("state") == "finalize"),
        },
        "smoke_sessions": sorted(smoke_sessions),
        "smoke_rows": len(smoke_rows),
        "paths": {key: os.path.abspath(path) for key, path in paths.items()},
        "checks": {
            "running": running_checks,
            "finalize": finalize_checks,
            "no_ttl": no_ttl_checks,
        },
        "problems": {
            "materialize_running": running_problems,
            "materialize_finalize": finalize_problems,
            "materialize_no_ttl": no_ttl_problems,
            "validate_running": running_validation,
            "validate_finalize": finalize_validation,
            "validate_no_ttl": no_ttl_validation,
        },
    }
    report_path = os.path.join(args.out_dir, "build_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps({
        "selected_sessions": report["selected_sessions"],
        "selected_rows": report["selected_rows"],
        "relaxed": relaxed,
        "selection_notes": selection_notes,
        "running_checks": running_checks["checks"],
        "finalize_checks": finalize_checks["checks"],
        "smoke_sessions": len(smoke_sessions),
        "report": report_path,
    }, ensure_ascii=False, indent=2))

    if not running_checks["checks"]["all_passed"] or not finalize_checks["checks"]["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
