#!/usr/bin/env python3
"""Add an inter-turn delay to a body dataset.

Agent turns are separated by tool execution in the real workload.  Without an
idle gap, the next turn is sent immediately and the engine keeps the session KV
in cache even without cache_control TTL, so TTL cannot show any benefit.

This script sets ``delay_after_ms`` on every row except the last row of each
session and marks those rows as ``tool`` state, because AISBench only honors
``delay_after_ms`` for pause/suspend/tool rows.  The AISBench session-aware
inferencer then waits that long before sending the next request of the same
session.
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
    p.add_argument("--delay-ms", type=int, default=30000)
    p.add_argument("--suffix", default=None)
    return p.parse_args()


def session_id_of(row: dict[str, Any]) -> str:
    hint = row.get("agent_hint") or {}
    nested = hint.get("session") or {}
    return str(nested.get("session_id") or hint.get("session_id") or "")


def process(path: str, delay_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    grouped: "OrderedDict[str, list[int]]" = OrderedDict()
    for index, row in enumerate(rows):
        grouped.setdefault(session_id_of(row), []).append(index)

    delayed = 0
    for indices in grouped.values():
        for index in indices[:-1]:
            rows[index]["delay_after_ms"] = delay_ms
            hint = rows[index].get("agent_hint")
            if isinstance(hint, dict):
                hint["agent_status"] = {"state": "tool"}
            delayed += 1
        if indices:
            rows[indices[-1]]["delay_after_ms"] = None

    report = {
        "input": os.path.abspath(path),
        "rows": len(rows),
        "sessions": len(grouped),
        "delay_ms": delay_ms,
        "delayed_rows": delayed,
    }
    return rows, report


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    args = parse_args()
    suffix = args.suffix or f"_delay{args.delay_ms // 1000}s"
    mapping = [
        ("hint_running_ttl_body_rot.jsonl", f"hint_running_ttl_body_rot{suffix}.jsonl"),
        ("hint_finalize_ttl_body_rot.jsonl", f"hint_finalize_ttl_body_rot{suffix}.jsonl"),
        ("hint_running_ttl_body_rot_smoke.jsonl", f"hint_running_ttl_body_rot{suffix}_smoke.jsonl"),
    ]
    reports = {}
    for src_name, dst_name in mapping:
        src_path = os.path.join(args.out_dir, src_name)
        if not os.path.exists(src_path):
            continue
        rows, report = process(src_path, args.delay_ms)
        dst_path = os.path.join(args.out_dir, dst_name)
        write_jsonl(dst_path, rows)
        report["output"] = os.path.abspath(dst_path)
        reports[dst_name] = report
        print(json.dumps(report, ensure_ascii=False))

    with open(os.path.join(args.out_dir, f"delay_report{suffix}.json"), "w", encoding="utf-8") as handle:
        json.dump(reports, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
