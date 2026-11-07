#!/usr/bin/env python3
"""Validate a TTFT Issue #52113 dataset with the real tokenizer.

Checks: prompt range, context fit, lifecycle states, finalize-only-last,
cache_control presence, header length, and per-session prefix continuity.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
from collections import OrderedDict
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output-report", default=None)
    p.add_argument("--tokenizer", default="/home/RAG_data/models/Qwen3-8B")
    p.add_argument("--max-context", type=int, default=32768)
    p.add_argument("--safety-margin", type=int, default=512)
    p.add_argument("--min-prompt", type=int, default=10000)
    p.add_argument("--max-prompt", type=int, default=30000)
    p.add_argument("--ttl-s", type=int, default=600)
    return p.parse_args()


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return json.loads(value) if value else None
    return value


def session_id_of(row: dict[str, Any]) -> str:
    session = (row.get("agent_hint") or {}).get("session") or {}
    return str(session.get("session_id") or "")


def header_len(hint: dict[str, Any]) -> int:
    raw = json.dumps(hint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(base64.urlsafe_b64encode(raw).rstrip(b"="))


def main() -> None:
    args = parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    rows = [json.loads(line) for line in open(args.input, encoding="utf-8-sig") if line.strip()]
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(session_id_of(row), []).append(row)

    problems: list[str] = []
    prompt_values: list[int] = []
    prefix_ratios: dict[str, float] = {}
    header_max = 0

    for sid, items in grouped.items():
        states = [((row.get("agent_hint") or {}).get("agent_status") or {}).get("state") for row in items]
        messages = [parse_json_field(row.get("messages")) for row in items]
        relations = []
        for prev, cur in zip(messages, messages[1:]):
            if not isinstance(prev, list) or not isinstance(cur, list):
                relations.append(False)
                continue
            n = min(len(prev), len(cur))
            relations.append(prev[:n] == cur[:n])
        prefix_ratios[sid] = sum(relations) / len(relations) if relations else 0.0

        if "finalize" in states[:-1]:
            problems.append(f"{sid}: finalize before last row")
        if states[-1] not in ("running", "finalize"):
            problems.append(f"{sid}: last state {states[-1]!r}")

        for turn, (row, state) in enumerate(zip(items, states)):
            hint = row.get("agent_hint") or {}
            header_max = max(header_max, header_len(hint))
            cache = hint.get("cache_control")
            if not isinstance(cache, dict) or cache.get("ttl_s") != args.ttl_s:
                problems.append(f"{sid} turn {turn}: missing/invalid cache_control")
            if state not in ("running", "finalize"):
                problems.append(f"{sid} turn {turn}: invalid state {state!r}")

            rendered = tokenizer.apply_chat_template(
                parse_json_field(row.get("messages")),
                tools=parse_json_field(row.get("tools")) or None,
                tokenize=True,
                add_generation_prompt=True,
            )
            if hasattr(rendered, "keys") and "input_ids" in rendered:
                rendered = rendered["input_ids"]
            prompt = len(rendered)
            prompt_values.append(prompt)
            if not (args.min_prompt <= prompt <= args.max_prompt):
                problems.append(f"{sid} turn {turn}: prompt {prompt} outside range")
            if prompt + int(row.get("max_out_len") or 0) + args.safety_margin > args.max_context:
                problems.append(
                    f"{sid} turn {turn}: context overflow prompt={prompt} "
                    f"max_out={row.get('max_out_len')}"
                )

    if header_max >= 16384:
        problems.append(f"header too long: {header_max}")

    report = {
        "input": os.path.abspath(args.input),
        "rows": len(rows),
        "sessions": len(grouped),
        "states": dict(
            (state, sum(
                1 for row in rows
                if ((row.get("agent_hint") or {}).get("agent_status") or {}).get("state") == state
            ))
            for state in ("running", "finalize")
        ),
        "prompt_tokens": {
            "min": min(prompt_values) if prompt_values else None,
            "median": statistics.median(prompt_values) if prompt_values else None,
            "max": max(prompt_values) if prompt_values else None,
        },
        "prefix_ratio": {
            "min": min(prefix_ratios.values()) if prefix_ratios else None,
            "median": statistics.median(prefix_ratios.values()) if prefix_ratios else None,
            "sessions_below_0_8": sorted(
                sid for sid, ratio in prefix_ratios.items() if ratio < 0.8
            ),
        },
        "header_len_max": header_max,
        "problems": problems,
        "all_passed": not problems,
    }
    out = args.output_report or (args.input + ".validate.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
