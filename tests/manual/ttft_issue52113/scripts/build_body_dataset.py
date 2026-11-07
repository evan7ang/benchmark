#!/usr/bin/env python3
"""Convert nested header datasets into flat body-hint datasets.

Motor under the load_balance policy disables TokenizerManager, so
attach_block_offsets cannot translate message indices into engine block
coordinates and the engine only ever protects block 0.  The documented
fallback is to send the legacy flat body hint with an explicit block_offset.

Body schema used here (legacy, not the v2 header spec):

    agent_hint = {
      "session_id": "...",
      "parent_session_id": "...",
      "agent_status": {"state": "running"|"finalize"},
      "cache_control": {"type": "ephemeral", "ttl": 600, "block_offset": N}
    }

`block_offset` is in 128-token engine block units: ceil(prompt_tokens / 128).
The engine (block_size=128, hash_block_size=128) then marks every prompt block
as ephemeral.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True)
    p.add_argument("--tokenizer", default="/home/RAG_data/models/Qwen3-8B")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--ttl-s", type=int, default=600)
    return p.parse_args()


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return json.loads(value) if value else None
    return value


def prompt_tokens(tokenizer, row: dict[str, Any]) -> int:
    messages = parse_json_field(row.get("messages"))
    tools = parse_json_field(row.get("tools"))
    rendered = tokenizer.apply_chat_template(
        messages, tools=tools or None, tokenize=True, add_generation_prompt=True
    )
    if hasattr(rendered, "keys") and "input_ids" in rendered:
        rendered = rendered["input_ids"]
    return len(rendered)


def convert(path: str, tokenizer, block_size: int, ttl_s: int, ttl: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    out: list[dict[str, Any]] = []
    offsets: list[int] = []
    for row in rows:
        hint = row.get("agent_hint") or {}
        nested = hint.get("session") or {}
        sid = str(nested.get("session_id") or hint.get("session_id") or "")
        parent = str(nested.get("parent_session_id") or sid)
        state = ((hint.get("agent_status") or {}).get("state")) or "running"
        tokens = prompt_tokens(tokenizer, row)
        block_offset = (tokens + block_size - 1) // block_size
        offsets.append(block_offset)

        body_hint: dict[str, Any] = {
            "session_id": sid,
            "parent_session_id": parent,
            "agent_status": {"state": state},
        }
        if ttl:
            body_hint["cache_control"] = {
                "type": "ephemeral",
                "ttl": ttl_s,
                "block_offset": block_offset,
            }
        row["agent_hint"] = body_hint
        row["max_out_len"] = min(int(row.get("max_out_len") or 512), 512)
        out.append(row)
    report = {
        "input": os.path.abspath(path),
        "rows": len(out),
        "sessions": len({(r.get("agent_hint") or {}).get("session_id") for r in out}),
        "states": {},
        "block_offset_min": min(offsets) if offsets else None,
        "block_offset_max": max(offsets) if offsets else None,
    }
    for row in out:
        state = ((row.get("agent_hint") or {}).get("agent_status") or {}).get("state")
        report["states"][state] = report["states"].get(state, 0) + 1
    return out, report


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    args = parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    src = args.out_dir
    mapping = [
        ("hint_running_ttl.jsonl", "hint_running_ttl_body.jsonl", True),
        ("hint_finalize_ttl.jsonl", "hint_finalize_ttl_body.jsonl", True),
        ("hint_running_no_ttl.jsonl", "hint_running_no_ttl_body.jsonl", False),
        ("hint_running_ttl_smoke.jsonl", "hint_running_ttl_body_smoke.jsonl", True),
    ]
    reports = {}
    for src_name, dst_name, ttl in mapping:
        src_path = os.path.join(src, src_name)
        if not os.path.exists(src_path):
            continue
        rows, report = convert(src_path, tokenizer, args.block_size, args.ttl_s, ttl)
        dst_path = os.path.join(src, dst_name)
        write_jsonl(dst_path, rows)
        report["output"] = os.path.abspath(dst_path)
        reports[dst_name] = report
        print(json.dumps(report, ensure_ascii=False))

    with open(os.path.join(src, "build_body_report.json"), "w", encoding="utf-8") as handle:
        json.dump(reports, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
