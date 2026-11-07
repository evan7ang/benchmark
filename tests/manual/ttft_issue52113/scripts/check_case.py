#!/usr/bin/env python3
"""Summarize one TTFT Issue #52113 case (generic, no KVC-specific PASS rules)."""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
from collections import Counter


def parse_details(out: str) -> dict:
    rows = ok = failed = 0
    states: Counter = Counter()
    for path in glob.glob(os.path.join(out, "aisbench", "**", "*_details.jsonl"), recursive=True):
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows += 1
            if row.get("success") is True and not row.get("error_info"):
                ok += 1
            else:
                failed += 1
            state = (row.get("extra_perf_data") or {}).get("lifecycle_state")
            if state:
                states[str(state).lower()] += 1
    return {"rows": rows, "ok": ok, "failed": failed, "states": dict(states)}


def csv_stats(path: str) -> dict[str, dict[str, float]]:
    if not os.path.exists(path):
        return {}
    columns: dict[str, list[float]] = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for key, raw in row.items():
                if key in ("ts", "date") or raw in ("", None):
                    continue
                try:
                    columns.setdefault(key, []).append(float(raw))
                except (TypeError, ValueError):
                    continue
    stats = {}
    for key, values in columns.items():
        values = sorted(values)
        stats[key] = {
            "min": values[0],
            "median": values[len(values) // 2],
            "max": values[-1],
            "delta": values[-1] - values[0],
        }
    return stats


def count_pattern(*paths: str, pattern: str) -> int:
    regex = re.compile(pattern)
    total = 0
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            if regex.search(line):
                total += 1
    return total


def aisbench_log_files(out: str) -> list[str]:
    files = [os.path.join(out, "aisbench.log")]
    files.extend(glob.glob(os.path.join(out, "aisbench", "**", "logs", "**", "*.out"), recursive=True))
    return [path for path in files if os.path.exists(path)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--engine-log", default="")
    parser.add_argument("--coord-log", default="")
    args = parser.parse_args()

    details = parse_details(args.out)
    metrics = csv_stats(os.path.join(args.out, "engine-metrics.csv"))
    run_rc_path = os.path.join(args.out, "run_rc.txt")
    run_rc = int(open(run_rc_path).read().strip()) if os.path.exists(run_rc_path) else -1
    ais_logs = aisbench_log_files(args.out)

    header_sends = count_pattern(*ais_logs, pattern=r"\[agent_hint\]\[send-header\]")
    body_sends = count_pattern(*ais_logs, pattern=r"\[agent_hint\]\[send\]")
    sam_sessions = count_pattern(args.engine_log, pattern=r"SAM: on_blocks_allocated_for_request.*session=")
    sam_protected = count_pattern(args.engine_log, pattern=r"newly_protected=[1-9]")
    motor_ttl = count_pattern(args.coord_log, pattern=r"cache_control|ttl")

    summary = {
        "case": args.case,
        "run_rc": run_rc,
        "details": details,
        "header_sends": header_sends,
        "body_sends": body_sends,
        "sam_session_allocs": sam_sessions,
        "sam_nonzero_newly_protected": sam_protected,
        "motor_cache_control_log_lines": motor_ttl,
        "kv_cache_usage_perc": metrics.get("engine_kv_cache_usage_perc", {}),
        "num_requests_waiting": metrics.get("engine_num_requests_waiting", {}),
        "num_preemptions_total": metrics.get("engine_num_preemptions_total", {}),
        "prefix_cache_hits_total": metrics.get("engine_prefix_cache_hits_total", {}),
        "prefix_cache_queries_total": metrics.get("engine_prefix_cache_queries_total", {}),
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    failed = run_rc != 0 or details["failed"] > 0
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
