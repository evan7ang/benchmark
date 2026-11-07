#!/usr/bin/env python3
"""Rotate the tools list per session, then rebuild flat body-hint datasets.

The capture gives 40/58 sessions the exact same 42.5K-char tools prefix
(sha1 1f1f4721).  That shared prefix is cached across sessions even in the
OFF arm, so it masks the session-specific KV retention that Agent Hint TTL is
supposed to protect.

Rotating the (unchanged, 27-entry) tools list by a deterministic per-session
offset keeps the prompt length in the 10-30K range while making the tools
prefix unique per session.  Later turns of a session still share the exact
same tools order, so intra-session prefix continuity is preserved.

The rotation is identical across OFF/ON/ON+Evict (it is a dataset property),
so the only arm difference remains the hint/state.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import OrderedDict
from typing import Any


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True)
    p.add_argument("--tokenizer", default="/home/RAG_data/models/Qwen3-8B")
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--ttl-s", type=int, default=600)
    p.add_argument("--max-out", type=int, default=512)
    return p.parse_args()


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return json.loads(value) if value else None
    return value


def session_id_of(row: dict[str, Any]) -> str:
    hint = row.get("agent_hint") or {}
    nested = hint.get("session") or {}
    return str(nested.get("session_id") or hint.get("session_id") or "")


def prompt_tokens(tokenizer, row: dict[str, Any]) -> int:
    messages = parse_json_field(row.get("messages"))
    tools = parse_json_field(row.get("tools"))
    rendered = tokenizer.apply_chat_template(
        messages, tools=tools or None, tokenize=True, add_generation_prompt=True
    )
    if hasattr(rendered, "keys") and "input_ids" in rendered:
        rendered = rendered["input_ids"]
    return len(rendered)


def build_session_index(out_dir: str) -> dict[str, int]:
    path = os.path.join(out_dir, "hint_running_ttl.jsonl")
    index: dict[str, int] = OrderedDict()
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        sid = session_id_of(json.loads(line))
        if sid not in index:
            index[sid] = len(index)
    return dict(index)


def convert(
    path: str,
    tokenizer,
    session_index: dict[str, int],
    block_size: int,
    ttl_s: int,
    max_out: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    out: list[dict[str, Any]] = []
    rotations: dict[str, int] = {}
    offsets: list[int] = []
    for row in rows:
        hint = row.get("agent_hint") or {}
        nested = hint.get("session") or {}
        sid = str(nested.get("session_id") or hint.get("session_id") or "")
        parent = str(nested.get("parent_session_id") or sid)
        state = ((hint.get("agent_status") or {}).get("state")) or "running"

        tools = parse_json_field(row.get("tools"))
        if isinstance(tools, list) and tools:
            rotation = session_index.get(sid, 0) % len(tools)
            rotations[sid] = rotation
            rotated = tools[rotation:] + tools[:rotation]
            row["tools"] = json.dumps(rotated, ensure_ascii=False, separators=(",", ":"))
        else:
            rotations[sid] = 0

        tokens = prompt_tokens(tokenizer, row)
        block_offset = (tokens + block_size - 1) // block_size
        offsets.append(block_offset)
        row["max_out_len"] = min(int(row.get("max_out_len") or max_out), max_out)

        body_hint: dict[str, Any] = {
            "session_id": sid,
            "parent_session_id": parent,
            "agent_status": {"state": state},
            "cache_control": {
                "type": "ephemeral",
                "ttl": ttl_s,
                "block_offset": block_offset,
            },
        }
        row["agent_hint"] = body_hint
        out.append(row)

    report = {
        "input": os.path.abspath(path),
        "rows": len(out),
        "sessions": len({(r.get("agent_hint") or {}).get("session_id") for r in out}),
        "prompt_tokens_min": min(offsets) * block_size if offsets else None,
        "block_offset_min": min(offsets) if offsets else None,
        "block_offset_max": max(offsets) if offsets else None,
        "rotations": rotations,
    }
    return out, report


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    args = parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    session_index = build_session_index(args.out_dir)
    mapping = [
        ("hint_running_ttl.jsonl", "hint_running_ttl_body_rot.jsonl"),
        ("hint_finalize_ttl.jsonl", "hint_finalize_ttl_body_rot.jsonl"),
        ("hint_running_ttl_smoke.jsonl", "hint_running_ttl_body_rot_smoke.jsonl"),
    ]
    reports = {}
    for src_name, dst_name in mapping:
        src_path = os.path.join(args.out_dir, src_name)
        if not os.path.exists(src_path):
            continue
        rows, report = convert(
            src_path,
            tokenizer,
            session_index,
            args.block_size,
            args.ttl_s,
            args.max_out,
        )
        dst_path = os.path.join(args.out_dir, dst_name)
        write_jsonl(dst_path, rows)
        report["output"] = os.path.abspath(dst_path)
        reports[dst_name] = report
        print(json.dumps({k: v for k, v in report.items() if k != "rotations"}, ensure_ascii=False))

    with open(os.path.join(args.out_dir, "build_rotated_report.json"), "w", encoding="utf-8") as handle:
        json.dump(reports, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
