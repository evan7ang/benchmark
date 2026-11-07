#!/usr/bin/env python3
"""Print a readable preview of an Agent Hint V2 JSONL dataset.

Long ``messages`` / ``tools`` strings are summarized (role, lengths, preview)
so a captured row can be inspected without dumping hundreds of KB.

Example:
    python -m ais_bench.capture.examples.preview_dataset \
      --dataset /path/to/dataset_v2.jsonl --rows 0,1,11 \
      --out /path/to/preview.json
"""
from __future__ import annotations

import argparse
import json
import os


def _preview(text, limit):
    if not isinstance(text, str):
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + f"... <{len(text) - limit} chars truncated>"


def _summarize_content(content, limit):
    if isinstance(content, str):
        return {"type": "text", "length": len(content), "preview": _preview(content, limit)}
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append({
                    "type": item.get("type"),
                    "length": len(str(item.get("text", ""))),
                    "preview": _preview(str(item.get("text", "")), limit),
                })
            else:
                parts.append({"type": type(item).__name__, "preview": _preview(str(item), limit)})
        return {"type": "list", "items": parts}
    return {"type": type(content).__name__, "preview": _preview(str(content), limit)}


def _summarize_message(message, limit):
    if not isinstance(message, dict):
        return {"preview": _preview(str(message), limit)}
    result = {
        "role": message.get("role"),
        "content": _summarize_content(message.get("content"), limit),
    }
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = (call or {}).get("function") or {}
            calls.append({
                "id": (call or {}).get("id"),
                "name": function.get("name"),
                "arguments": _preview(str(function.get("arguments", "")), limit),
            })
        result["tool_calls"] = calls
    if message.get("tool_call_id"):
        result["tool_call_id"] = message["tool_call_id"]
    if message.get("name"):
        result["name"] = message["name"]
    return result


def _summarize_tools(tools, limit):
    summaries = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            summaries.append({"preview": _preview(str(tool), limit)})
            continue
        function = tool.get("function") or {}
        summaries.append({
            "type": tool.get("type"),
            "name": function.get("name") or tool.get("name"),
            "description": _preview(str(function.get("description") or tool.get("description") or ""), limit),
        })
    return summaries


def preview_row(row, index, line, limit):
    messages = []
    raw_messages = row.get("messages")
    if isinstance(raw_messages, str):
        try:
            raw_messages = json.loads(raw_messages)
        except Exception:
            raw_messages = []
    for message in raw_messages or []:
        messages.append(_summarize_message(message, limit))

    tools = row.get("tools")
    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except Exception:
            tools = []
    tools = tools or []

    top_level = {
        key: value for key, value in row.items()
        if key not in ("messages", "tools", "agent_hint")
    }
    return {
        "line_no": index,
        "question": row.get("question"),
        "max_out_len": row.get("max_out_len"),
        "timestamp": row.get("timestamp"),
        "agent_hint": row.get("agent_hint"),
        "top_level_fields": top_level,
        "message_count": len(messages),
        "messages": messages,
        "tool_count": len(tools),
        "tools": _summarize_tools(tools, limit),
        "raw_line_bytes": len(line),
        "raw_line_prefix": line[:limit] + " ..." if len(line) > limit else line,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--rows", default="0,1,2",
                    help="Comma-separated 0-based row indices (default: 0,1,2)")
    ap.add_argument("--limit", type=int, default=240,
                    help="Preview characters per text/tool/argument (default: 240)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    wanted = {int(item) for item in args.rows.split(",") if item.strip()}
    rows = []
    with open(args.dataset, encoding="utf-8") as f:
        for index, line in enumerate(f):
            if index not in wanted:
                continue
            line = line.rstrip("\n")
            rows.append(preview_row(json.loads(line), index, line, args.limit))

    result = {
        "dataset": os.path.abspath(args.dataset),
        "sample_rows": sorted(rows, key=lambda item: item["line_no"]),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
