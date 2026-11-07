#!/usr/bin/env python3
"""Tokenizer-level context validation for manual KVC datasets.

Runs inside the AISBench runner container (which has transformers). It keeps
session lifecycles coherent: a session is dropped entirely if any of its rows
cannot fit the context window after clamping max_out_len.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import OrderedDict
from typing import Any


def session_of(row: dict[str, Any]) -> str:
    return str(((row.get("agent_hint") or {}).get("session") or {}).get("session_id") or "")


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        return json.loads(value)
    return value


def prompt_tokens(tokenizer, row: dict[str, Any]) -> int:
    messages = parse_json_field(row.get("messages"))
    tools = parse_json_field(row.get("tools"))
    if not messages:
        text = str(row.get("question") or "")
        return len(tokenizer(text, add_special_tokens=True)["input_ids"])
    tokens = tokenizer.apply_chat_template(
        messages,
        tools=tools or None,
        tokenize=True,
        add_generation_prompt=True,
    )
    # Depending on the transformers version this is either a list of ids or a
    # BatchEncoding with an "input_ids" entry.
    if hasattr(tokens, "keys") and "input_ids" in tokens:
        tokens = tokens["input_ids"]
    return len(tokens)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tokenizer", default=os.environ.get("TOKENIZER_PATH") or os.environ.get("MODEL_PATH", ""))
    parser.add_argument("--max-context", type=int, default=int(os.environ.get("MAX_MODEL_LEN", "32768")))
    parser.add_argument("--min-max-out", type=int, default=32)
    parser.add_argument("--safety-margin", type=int, default=512)
    args = parser.parse_args()

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    rows = [json.loads(line) for line in open(args.input, encoding="utf-8") if line.strip()]
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(session_of(row), []).append(row)

    kept: list[dict[str, Any]] = []
    dropped_sessions: list[dict[str, Any]] = []
    clamped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for sid, items in grouped.items():
        planned: list[dict[str, Any]] = []
        drop_session = False
        for row in items:
            new_row = json.loads(json.dumps(row))
            try:
                tokens = prompt_tokens(tokenizer, new_row)
            except Exception as exc:  # noqa: BLE001 - report, do not guess
                errors.append({"session_id": sid, "error": str(exc)})
                drop_session = True
                break
            tools = parse_json_field(new_row.get("tools"))
            if not tools:
                # vLLM rejects tool_choice without tools; AISBench already
                # drops an empty tools array, so drop the dangling choice too.
                new_row["tool_choice"] = None
            max_out = int(new_row.get("max_out_len") or 0)
            if tokens + max_out + args.safety_margin <= args.max_context:
                planned.append(new_row)
                continue
            new_max = args.max_context - tokens - args.safety_margin
            if new_max < args.min_max_out:
                drop_session = True
                break
            new_row["max_out_len"] = new_max
            clamped.append(
                {
                    "session_id": sid,
                    "old_max_out_len": max_out,
                    "new_max_out_len": new_max,
                    "prompt_tokens": tokens,
                }
            )
            planned.append(new_row)
        if drop_session:
            dropped_sessions.append({"session_id": sid, "rows": len(items)})
        else:
            kept.extend(planned)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    report = {
        "input": args.input,
        "output": args.output,
        "tokenizer": args.tokenizer,
        "max_context": args.max_context,
        "min_max_out": args.min_max_out,
        "input_rows": len(rows),
        "output_rows": len(kept),
        "input_sessions": len(grouped),
        "output_sessions": len({session_of(r) for r in kept}),
        "dropped_sessions": dropped_sessions,
        "clamped_rows": clamped,
        "errors": errors,
    }
    with open(args.output + ".report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("input_rows", "output_rows", "input_sessions", "output_sessions")}))
    if dropped_sessions:
        print("dropped_sessions:", dropped_sessions)


if __name__ == "__main__":
    main()
