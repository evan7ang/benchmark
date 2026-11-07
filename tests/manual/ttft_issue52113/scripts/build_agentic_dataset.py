#!/usr/bin/env python3
"""Build a synthetic agentic dataset that matches the Agent Hint scenario.

Why synthetic:
  The captured v2 traces do not contain enough sessions with large per-turn
  context growth, and their tool gaps are not represented.  For an Agent Hint
  TTL experiment we need:

    - 20 sessions, 5-7 turns each, 10-30K prompt tokens;
    - session-specific tools / prefix (no dominant cross-session prefix);
    - 2-5K new tokens per turn (tool output), so the session prefix is large
      and worth protecting;
    - pause/resume gaps that RELEASE the session slot, so other sessions run
      while one session is idle and can evict its KV in the OFF arm.

The generated conversation is a valid Moonshot AI-style agent transcript:
system -> user task -> (assistant tool_call -> tool output -> user follow-up)*

Outputs flat body-hint datasets:
    agent_hint = {
      "session_id": ..., "parent_session_id": ...,
      "agent_status": {"state": "pause|resume|running|finalize"},
      "cache_control": {"type":"ephemeral","ttl":600,"block_offset":N}
    }
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import statistics
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_finalize.jsonl")
    p.add_argument("--out-dir", default="/home/t00887700/benchmark-data/agent_replay_v2/ttft_issue52113")
    p.add_argument("--tokenizer", default="/home/RAG_data/models/Qwen3-8B")
    p.add_argument("--sessions", type=int, default=20)
    p.add_argument("--tools-count", type=int, default=15)
    p.add_argument("--delta-tokens", type=int, default=3000)
    p.add_argument("--max-deltas", type=int, default=6)
    p.add_argument("--min-prompt", type=int, default=10000)
    p.add_argument("--max-prompt", type=int, default=30000)
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--safety-margin", type=int, default=512)
    p.add_argument("--max-out", type=int, default=512)
    p.add_argument("--delay-ms", type=int, default=60000)
    p.add_argument("--ttl-s", type=int, default=600)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--smoke-sessions", type=int, default=3)
    return p.parse_args()


def load_source(path: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    with open(path, encoding="utf-8-sig") as handle:
        row = json.loads(handle.readline())
    messages = json.loads(row["messages"])
    tools = json.loads(row["tools"])
    system = messages[0]["content"]
    users = [m for m in messages if m.get("role") == "user"]
    return system, tools, users


def count_prompt(tokenizer, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    rendered = tokenizer.apply_chat_template(
        messages,
        tools=tools or None,
        tokenize=True,
        add_generation_prompt=True,
    )
    if hasattr(rendered, "keys") and "input_ids" in rendered:
        rendered = rendered["input_ids"]
    return len(rendered)


def make_unique_text(prefix: str, target_tokens: int, tokenizer) -> str:
    """Generate a deterministic, session/turn-unique code-like text."""
    lines: list[str] = []
    index = 0
    while True:
        for _ in range(20):
            digest = hashlib.sha1(f"{prefix}:{index}".encode("utf-8")).hexdigest()[:10]
            lines.append(f"# {prefix} line {index} id {digest}")
            lines.append(f"value_{index}_{digest} = transform_{index}(input_{digest})  # checksum {digest}")
            index += 1
        content = "\n".join(lines)
        tokens = len(tokenizer(content, add_special_tokens=False)["input_ids"])
        if tokens >= target_tokens:
            return content
        if index > 4000:
            return content


def build_session(
    tokenizer,
    source_system: str,
    source_tools: list[dict[str, Any]],
    source_users: list[dict[str, Any]],
    session_index: int,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sid = f"synth_{session_index:02d}_{hashlib.sha1(str(session_index).encode()).hexdigest()[:8]}"
    start = session_index % len(source_tools)
    tools = (source_tools[start:] + source_tools[:start])[: args.tools_count]

    system = (
        f"# Identity\nYou are a coding agent working in /workspace/{sid}. "
        f"Use the provided tools to inspect the repository and fix defects.\n\n"
        f"# Rules\n- Read before editing.\n- Run tests after each change.\n"
        f"- Keep changes minimal and consistent with the existing style.\n"
        f"- Session: {sid}\n"
    )
    base_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": source_users[0]["content"]},
        {"role": "user", "content": source_users[1]["content"]},
    ]
    base_tokens = count_prompt(tokenizer, base_messages, tools)
    if base_tokens < args.min_prompt:
        filler = make_unique_text(
            f"{sid}:overview", args.min_prompt - base_tokens + 200, tokenizer
        )
        base_messages.append({"role": "user", "content": "Repository overview:\n" + filler})
        base_tokens = count_prompt(tokenizer, base_messages, tools)

    rows: list[list[dict[str, Any]]] = [base_messages]
    prompt_counts: list[int] = [base_tokens]
    deltas: list[int] = []

    current = base_messages
    for turn in range(1, args.max_deltas + 1):
        tool_text = make_unique_text(f"{sid}:turn{turn}", args.delta_tokens, tokenizer)
        assistant = {
            "role": "assistant",
            "content": f"<think>Round {turn}: inspect the repository and fix the next issue.</think>",
            "tool_calls": [
                {
                    "function": {
                        "arguments": json.dumps(
                            {
                                "command": f"python -m pytest tests/test_{turn}.py",
                                "description": f"run tests round {turn}",
                                "timeout": 300,
                                "workdir": f"/workspace/{sid}",
                            },
                            ensure_ascii=False,
                        ),
                        "name": "bash",
                    },
                    "id": f"call_{sid}_{turn}",
                    "index": None,
                    "type": "function",
                }
            ],
        }
        tool_msg = {
            "role": "tool",
            "tool_call_id": f"call_{sid}_{turn}",
            "content": tool_text,
        }
        user_msg = {
            "role": "user",
            "content": f"Round {turn + 1}: continue. Analyze the command output above and fix the next defect.",
        }
        candidate = current + [assistant, tool_msg, user_msg]
        tokens = count_prompt(tokenizer, candidate, tools)
        if tokens > args.max_prompt:
            break
        deltas.append(tokens - prompt_counts[-1])
        rows.append(candidate)
        prompt_counts.append(tokens)
        current = candidate

    report = {
        "session_id": sid,
        "tools_start": start,
        "tools_count": len(tools),
        "turns": len(rows),
        "prompt_counts": prompt_counts,
        "deltas": deltas,
        "first_prompt": prompt_counts[0],
        "last_prompt": prompt_counts[-1],
    }
    return rows, report


def make_body_rows(
    rows: list[list[dict[str, Any]]],
    report: dict[str, Any],
    tools: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    finalize: bool,
) -> list[dict[str, Any]]:
    sid = report["session_id"]
    out: list[dict[str, Any]] = []
    last = len(rows) - 1
    for index, (messages, tokens) in enumerate(zip(rows, report["prompt_counts"])):
        if index == last:
            state = "finalize" if finalize else "running"
        elif index % 2 == 0:
            state = "pause"
        else:
            state = "resume"
        delay = args.delay_ms if state == "pause" else None
        block_offset = math.ceil(tokens / args.block_size)
        hint = {
            "session_id": sid,
            "parent_session_id": sid,
            "agent_status": {"state": state},
            "cache_control": {
                "type": "ephemeral",
                "ttl": args.ttl_s,
                "block_offset": block_offset,
            },
        }
        out.append(
            {
                "question": f"Session {sid} round {index}.",
                "max_out_len": args.max_out,
                "timestamp": 0,
                "messages": json.dumps(messages, ensure_ascii=False),
                "tools": json.dumps(tools, ensure_ascii=False),
                "agent_hint": hint,
                "delay_after_ms": delay,
            }
        )
    return out


def validate(rows: list[dict[str, Any]], tokenizer, args: argparse.Namespace) -> dict[str, Any]:
    problems: list[str] = []
    prompt_values: list[int] = []
    deltas: list[int] = []
    header_max = 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["agent_hint"]["session_id"], []).append(row)
    for sid, items in grouped.items():
        previous_messages = None
        previous_tokens = None
        for index, row in enumerate(items):
            messages = json.loads(row["messages"])
            tools = json.loads(row["tools"])
            tokens = count_prompt(tokenizer, messages, tools)
            prompt_values.append(tokens)
            if not (args.min_prompt <= tokens <= args.max_prompt):
                problems.append(f"{sid} row {index}: prompt {tokens} out of range")
            if previous_messages is not None:
                if messages[: len(previous_messages)] != previous_messages:
                    problems.append(f"{sid} row {index}: prefix continuity broken")
                deltas.append(tokens - previous_tokens)
            previous_messages = messages
            previous_tokens = tokens
            hint = row["agent_hint"]
            header = base64.urlsafe_b64encode(
                json.dumps(hint, ensure_ascii=False, separators=(",", ":")).encode()
            ).rstrip(b"=")
            header_max = max(header_max, len(header))
            if hint.get("agent_status", {}).get("state") in ("pause", "resume", "running", "finalize"):
                pass
            else:
                problems.append(f"{sid} row {index}: bad state")
            if tokens + row["max_out_len"] + args.safety_margin > args.max_context:
                problems.append(f"{sid} row {index}: context overflow")
    report = {
        "rows": len(rows),
        "sessions": len(grouped),
        "prompt_tokens": {
            "min": min(prompt_values) if prompt_values else None,
            "median": statistics.median(prompt_values) if prompt_values else None,
            "max": max(prompt_values) if prompt_values else None,
        },
        "delta_tokens": {
            "min": min(deltas) if deltas else None,
            "median": statistics.median(deltas) if deltas else None,
            "max": max(deltas) if deltas else None,
        },
        "header_len_max": header_max,
        "problems": problems,
        "all_passed": not problems,
    }
    return report


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    args = parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    source_system, source_tools, source_users = load_source(args.source)

    all_running: list[dict[str, Any]] = []
    all_finalize: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for i in range(args.sessions):
        rows, report = build_session(
            tokenizer, source_system, source_tools, source_users, i, args
        )
        if len(rows) < 5:
            raise SystemExit(f"session {i}: only {len(rows)} rows; lower delta or min prompt")
        # Recover the tools subset exactly as build_session did.
        start = i % len(source_tools)
        tools = (source_tools[start:] + source_tools[:start])[: args.tools_count]
        all_running.extend(make_body_rows(rows, report, tools, args, finalize=False))
        all_finalize.extend(make_body_rows(rows, report, tools, args, finalize=True))
        reports.append(report)

    running_report = validate(all_running, tokenizer, args)
    finalize_report = validate(all_finalize, tokenizer, args)

    os.makedirs(args.out_dir, exist_ok=True)
    paths = {
        "running": os.path.join(args.out_dir, "synth_running_body_pause.jsonl"),
        "finalize": os.path.join(args.out_dir, "synth_finalize_body_pause.jsonl"),
        "running_smoke": os.path.join(args.out_dir, "synth_running_body_pause_smoke.jsonl"),
    }
    write_jsonl(paths["running"], all_running)
    write_jsonl(paths["finalize"], all_finalize)
    smoke_sessions = {r["session_id"] for r in reports[: args.smoke_sessions]}
    write_jsonl(paths["running_smoke"], [r for r in all_running if r["agent_hint"]["session_id"] in smoke_sessions])

    report = {
        "source": os.path.abspath(args.source),
        "tokenizer": os.path.abspath(args.tokenizer),
        "args": vars(args),
        "sessions": args.sessions,
        "session_reports": reports,
        "running": running_report,
        "finalize": finalize_report,
        "paths": {k: os.path.abspath(v) for k, v in paths.items()},
    }
    with open(os.path.join(args.out_dir, "synth_build_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps({
        "sessions": args.sessions,
        "rows": len(all_running),
        "turns_per_session": [r["turns"] for r in reports],
        "prompt_tokens": running_report["prompt_tokens"],
        "delta_tokens": running_report["delta_tokens"],
        "all_passed": running_report["all_passed"] and finalize_report["all_passed"],
        "problems": (running_report["problems"] + finalize_report["problems"])[:10],
        "paths": report["paths"],
    }, ensure_ascii=False, indent=2))
    if not (running_report["all_passed"] and finalize_report["all_passed"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
