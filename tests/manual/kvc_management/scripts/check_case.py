#!/usr/bin/env python3
"""Summarize one KVC manual case and decide PASS/FAIL."""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
from collections import Counter
from typing import Any

KVC_KEYS = [
    "offload_total",
    "prefetch_total",
    "evict_total",
    "edit_success_total",
    "edit_fail_total",
    "send_fail_total",
    "gate_skip_total",
    "queue_full_total",
]


def parse_prom(path: str) -> dict[str, float]:
    values: dict[str, float] = {}
    if not os.path.exists(path):
        return values
    for line in open(path, encoding="utf-8", errors="replace"):
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name_part, raw = parts
        name = name_part.split("{", 1)[0]
        if not name.startswith("motor:kvc_"):
            continue
        key = name[len("motor:kvc_") :]
        try:
            values[key] = values.get(key, 0.0) + float(raw)
        except ValueError:
            continue
    return values


def motor_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    delta: dict[str, float] = {}
    for key in KVC_KEYS:
        b = before.get(key, 0.0)
        a = after.get(key, 0.0)
        delta[key] = a - b if a >= b else a
    return delta


def _read_offset_file(path: str) -> dict[str, int]:
    vals: dict[str, int] = {}
    if not os.path.exists(path):
        return vals
    for line in open(path, encoding="utf-8"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            try:
                vals[k] = int(v)
            except ValueError:
                pass
    return vals


def read_offsets(out: str) -> tuple[int, int, int | None, int | None]:
    starts = _read_offset_file(os.path.join(out, "log-offsets.txt"))
    ends = _read_offset_file(os.path.join(out, "log-offsets-end.txt"))
    return starts.get("engine", 0), starts.get("coord", 0), ends.get("engine"), ends.get("coord")


def read_new_lines(path: str, start: int, end: int | None = None) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return lines[start:end]


def engine_edits(lines: list[str]) -> tuple[Counter, int]:
    counts: Counter = Counter()
    blocks = 0
    for line in lines:
        m = re.search(r"Executing edit type=(\w+)", line)
        if m:
            counts[m.group(1)] += 1
        m2 = re.search(r"actual_op_block_num\s+(\d+)", line)
        if m2:
            blocks += int(m2.group(1))
    return counts, blocks


def aisbench_log_text(out: str) -> str:
    chunks: list[str] = []
    main = os.path.join(out, "aisbench.log")
    if os.path.exists(main):
        chunks.append(open(main, encoding="utf-8", errors="replace").read())
    for path in glob.glob(os.path.join(out, "aisbench", "**", "logs", "**", "*.out"), recursive=True):
        chunks.append(open(path, encoding="utf-8", errors="replace").read())
    return "\n".join(chunks)


def agent_status_counts(text: str) -> Counter:
    counts: Counter = Counter()
    for m in re.finditer(r"\[agent_hint\]\[agent-status\].*?state=(\w+)", text):
        counts[m.group(1)] += 1
    return counts


def parse_details(out: str) -> dict[str, Any]:
    rows = 0
    ok = 0
    failed = 0
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
        reader = csv.DictReader(handle)
        for row in reader:
            for key, raw in row.items():
                if key in ("ts", "date") or raw in ("", None):
                    continue
                try:
                    columns.setdefault(key, []).append(float(raw))
                except (TypeError, ValueError):
                    continue
    stats: dict[str, dict[str, float]] = {}
    for key, values in columns.items():
        values_sorted = sorted(values)
        stats[key] = {
            "count": len(values),
            "min": values_sorted[0],
            "median": values_sorted[len(values_sorted) // 2],
            "max": values_sorted[-1],
            "delta": values_sorted[-1] - values_sorted[0],
        }
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--engine-log", default="")
    parser.add_argument("--coord-log", default="")
    args = parser.parse_args()

    before = parse_prom(os.path.join(args.out, "motor-before.prom"))
    after = parse_prom(os.path.join(args.out, "motor-after.prom"))
    delta = motor_delta(before, after)

    engine_start, coord_start, engine_end, coord_end = read_offsets(args.out)
    e_lines = read_new_lines(args.engine_log, engine_start, engine_end)
    c_lines = read_new_lines(args.coord_log, coord_start, coord_end)
    e_counts, e_blocks = engine_edits(e_lines)
    coord_counts = Counter()
    for line in c_lines:
        for action in ("offload", "prefetch", "evict"):
            if f"kvc {action} done" in line:
                coord_counts[action] += 1

    text = aisbench_log_text(args.out)
    posts = agent_status_counts(text)
    details = parse_details(args.out)
    metrics = csv_stats(os.path.join(args.out, "engine-metrics.csv"))
    run_rc_path = os.path.join(args.out, "run_rc.txt")
    run_rc = int(open(run_rc_path).read().strip()) if os.path.exists(run_rc_path) else -1

    kv = metrics.get("engine_kv_cache_usage_perc", {})
    wait = metrics.get("engine_num_requests_waiting", {})
    preempt = metrics.get("engine_num_preemptions_total", {})
    running = metrics.get("engine_num_requests_running", {})

    case = args.case
    # qa_run.sh uses short IDs (S1..S7); the per-case assertions use semantic
    # names. Accept both so a consolidated run applies the same checks as a
    # single-case run.
    aliases = {
        "s1": "ramp",
        "s2": "finalize_evict",
        "s3": "pause_offload",
        "s5": "saturated",
        "s6": "kvc_off",
        "s7": "gate",
    }
    case_type = aliases.get(case.lower(), case)
    reasons: list[str] = []
    pass_ = True

    def require(cond: bool, msg: str) -> None:
        nonlocal pass_
        if not cond:
            pass_ = False
            reasons.append(msg)

    if not case_type.startswith("ramp") and case_type != "saturated":
        require(run_rc == 0, f"aisbench exit code {run_rc}")
        require(details["failed"] == 0, f"failed requests {details['failed']}")

    if case_type == "finalize_evict":
        require(delta["evict_total"] > 0, "evict_total did not increase")
        require(delta["offload_total"] == 0, "offload unexpectedly increased")
        require(delta["prefetch_total"] == 0, "prefetch unexpectedly increased")
        require(delta["edit_fail_total"] == 0, "edit_fail > 0")
        require(delta["send_fail_total"] == 0, "send_fail > 0")
        require(e_counts.get("evict", 0) > 0, "engine has no evict edit")
        require(posts.get("finalize", 0) > 0, "no finalize status posts")
    elif case_type == "pause_offload":
        require(delta["offload_total"] > 0, "offload_total did not increase")
        require(delta["prefetch_total"] > 0, "prefetch_total did not increase")
        require(delta["prefetch_total"] <= delta["offload_total"], "prefetch_total > offload_total")
        require(delta["edit_fail_total"] == 0, "edit_fail > 0")
        require(delta["send_fail_total"] == 0, "send_fail > 0")
        require(e_counts.get("offload", 0) > 0, "engine has no offload edit")
        require(e_counts.get("prefetch", 0) > 0, "engine has no prefetch edit")
        require(posts.get("pause", 0) > 0, "no pause status posts")
        # Under the four-state KVC contract (feature/agent-hint-kvc-lifecycle)
        # the resume signal is posted as wire state "resume"; older benches
        # folded it into "running". Accept either.
        require(posts.get("running", 0) + posts.get("resume", 0) > 0, "no resume/running status posts")
    elif case_type == "saturated":
        require(delta["offload_total"] > 0, "offload_total did not increase under saturation")
        require(delta["edit_fail_total"] == 0, "edit_fail > 0")
        require(delta["send_fail_total"] == 0, "send_fail > 0")
        require(
            kv.get("max", 0.0) >= 0.7 or wait.get("max", 0.0) > 0,
            "KV pressure did not reach the saturation criterion",
        )
    elif case_type == "kvc_off":
        require(all(delta[k] == 0 for k in KVC_KEYS), "KVC counters changed while KVC is off")
        require(sum(e_counts.values()) == 0, "engine processed edits while KVC is off")
        require(posts.get("pause", 0) > 0, "no pause status posts")
        require(posts.get("finalize", 0) > 0, "no finalize status posts")
    elif case_type == "gate":
        require(delta["gate_skip_total"] > 0, "gate_skip did not increase")
        require(delta["offload_total"] == 0, "offload happened despite high-water gate")
        require(delta["edit_fail_total"] == 0, "edit_fail > 0")
    elif case_type.startswith("ramp"):
        kv_max = kv.get("max", 0.0)
        wait_max = wait.get("max", 0.0)
        pre_max = preempt.get("delta", 0.0)
        require(
            kv_max >= 0.7 or wait_max > 0 or pre_max > 0,
            "KV pressure did not reach the saturation criterion",
        )
    else:
        require(details["rows"] > 0, "no AISBench details")

    summary = {
        "case": case,
        "pass": pass_,
        "reasons": reasons,
        "aisbench": {
            "exit_code": run_rc,
            "details": details,
            "agent_status_posts": dict(posts),
        },
        "motor_kvc_delta": delta,
        "engine_edits": {
            "counts": dict(e_counts),
            "actual_op_block_num_sum": e_blocks,
        },
        "coordinator_done": dict(coord_counts),
        "engine_metrics": {
            "kv_cache_usage_perc": kv,
            "num_requests_waiting": wait,
            "num_preemptions_total": preempt,
            "num_requests_running": running,
        },
    }

    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)

    kv_text = ""
    if kv:
        kv_text = (
            f" kv_usage_p50={kv['median'] * 100:.1f}% kv_usage_max={kv['max'] * 100:.1f}%"
            f" waiting_max={wait.get('max', 0):.0f} preemptions={preempt.get('delta', 0):.0f}"
        )

    lines = [
        f"case: {case}",
        f"PASS: {pass_}",
        f"aisbench: exit={run_rc} rows={details['rows']} ok={details['ok']} failed={details['failed']}",
        f"agent-status posts: pause={posts.get('pause', 0)} running={posts.get('running', 0)} finalize={posts.get('finalize', 0)}",
        "motor kvc delta: " + ", ".join(f"{k}={delta[k]:.0f}" for k in KVC_KEYS),
        "engine edits: " + ", ".join(f"{k}={v}" for k, v in sorted(e_counts.items())) + f" blocks={e_blocks}",
        f"coordinator done: " + ", ".join(f"{k}={v}" for k, v in sorted(coord_counts.items())),
        f"engine metrics:{kv_text}",
    ]
    if reasons:
        lines.append("reasons: " + "; ".join(reasons))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
