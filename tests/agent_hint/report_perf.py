#!/usr/bin/env python3
"""Build a compact Markdown/JSON report for Agent Hint ON/OFF performance runs.

The input is one or two AISBench ``performances`` trees (or their parent work
directories). Optional Prometheus snapshots add Motor KVC counters.

Example:
    python3 tests/agent_hint/report_perf.py \
      --on-run outputs/agent-hint/on \
      --off-run outputs/agent-hint/off \
      --motor-before on/motor-before.prom --motor-after on/motor-after.prom \
      --out outputs/agent-hint/report
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

PERF_KEYS = ["TTFT", "E2EL", "TPOT", "ITL", "OutputTokenThroughput", "PrefillTokenThroughput"]
MOTOR_KVC_KEYS = [
    "offload_total",
    "prefetch_total",
    "evict_total",
    "edit_success_total",
    "edit_fail_total",
    "send_fail_total",
    "gate_skip_total",
    "queue_full_total",
]


def _find_one(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.rglob(pattern))
    return matches[0] if matches else None


def _parse_csv(path: Path) -> dict[str, dict[str, str]]:
    metrics: dict[str, dict[str, str]] = {}
    if not path or not path.exists():
        return metrics
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Performance Parameters") or row.get("metric") or ""
            if name:
                metrics[name] = row
    return metrics


def _parse_common(path: Path) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for key in (
        "Benchmark Duration",
        "Total Requests",
        "Failed Requests",
        "Success Requests",
        "Concurrency",
        "Max Concurrency",
        "Request Throughput",
        "Output Token Throughput",
    ):
        value = data.get(key)
        if isinstance(value, dict) and "total" in value:
            out[key] = value["total"]
    return out


def _parse_prom(path: Path | None) -> dict[str, float]:
    values: dict[str, float] = {}
    if not path or not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name = parts[0].split("{", 1)[0]
        if not name.startswith("motor:kvc_"):
            continue
        key = name[len("motor:kvc_") :]
        try:
            values[key] = values.get(key, 0.0) + float(parts[1])
        except ValueError:
            continue
    return values


def _kvc_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    delta = {}
    for key in MOTOR_KVC_KEYS:
        b = before.get(key, 0.0)
        a = after.get(key, 0.0)
        delta[key] = a - b if a >= b else a
    return delta


def _arm(run_dir: Path | None, motor_before: Path | None, motor_after: Path | None) -> dict[str, Any]:
    if run_dir is None:
        return {}
    run_dir = run_dir.resolve()
    csv_path = _find_one(run_dir, "*.csv")
    json_path = _find_one(run_dir, "*.json")
    details_path = _find_one(run_dir, "*_details.jsonl")
    arm = {
        "run_dir": str(run_dir),
        "metrics_csv": str(csv_path) if csv_path else None,
        "common": _parse_common(json_path) if json_path else {},
        "metrics": _parse_csv(csv_path) if csv_path else {},
        "details": None,
    }
    if details_path:
        rows = 0
        sessions: set[str] = set()
        states: dict[str, int] = {}
        for line in details_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows += 1
            if row.get("session_id"):
                sessions.add(str(row["session_id"]))
            state = str((row.get("extra_perf_data") or {}).get("lifecycle_state") or "")
            if state:
                states[state] = states.get(state, 0) + 1
        arm["details"] = {"rows": rows, "sessions": len(sessions), "states": states}
    if motor_before or motor_after:
        arm["kvc_delta"] = _kvc_delta(_parse_prom(motor_before), _parse_prom(motor_after))
    return arm


def _metric_value(arm: dict[str, Any], metric: str, column: str) -> str:
    row = (arm.get("metrics") or {}).get(metric) or {}
    return str(row.get(column, ""))


def _render_markdown(title: str, on: dict[str, Any], off: dict[str, Any], generated: str) -> str:
    lines = [f"# {title}", "", f"Generated: {generated}", ""]
    lines += ["## ON / OFF summary", ""]
    lines += [
        "| Item | ON | OFF |",
        "|---|---|---|",
        f"| Requests (success/total) | {on.get('common', {}).get('Success Requests', '')}/{on.get('common', {}).get('Total Requests', '')} | {off.get('common', {}).get('Success Requests', '')}/{off.get('common', {}).get('Total Requests', '')} |",
        f"| Max Concurrency | {on.get('common', {}).get('Max Concurrency', '')} | {off.get('common', {}).get('Max Concurrency', '')} |",
        f"| Benchmark Duration | {on.get('common', {}).get('Benchmark Duration', '')} | {off.get('common', {}).get('Benchmark Duration', '')} |",
        f"| Output Token Throughput | {on.get('common', {}).get('Output Token Throughput', '')} | {off.get('common', {}).get('Output Token Throughput', '')} |",
        f"| TTFT avg / P50 / P90 / P99 | {_metric_value(on, 'TTFT', 'Average')} / {_metric_value(on, 'TTFT', 'Median')} / {_metric_value(on, 'TTFT', 'P90')} / {_metric_value(on, 'TTFT', 'P99')} | {_metric_value(off, 'TTFT', 'Average')} / {_metric_value(off, 'TTFT', 'Median')} / {_metric_value(off, 'TTFT', 'P90')} / {_metric_value(off, 'TTFT', 'P99')} |",
        f"| E2EL avg / P90 / P99 | {_metric_value(on, 'E2EL', 'Average')} / {_metric_value(on, 'E2EL', 'P90')} / {_metric_value(on, 'E2EL', 'P99')} | {_metric_value(off, 'E2EL', 'Average')} / {_metric_value(off, 'E2EL', 'P90')} / {_metric_value(off, 'E2EL', 'P99')} |",
        f"| TPOT avg | {_metric_value(on, 'TPOT', 'Average')} | {_metric_value(off, 'TPOT', 'Average')} |",
        "",
    ]
    for label, arm in (("ON", on), ("OFF", off)):
        if not arm:
            continue
        lines += [f"## {label} details", ""]
        lines += [f"- run: `{arm.get('run_dir')}`"]
        if arm.get("details"):
            lines += [
                f"- details rows: {arm['details']['rows']}, sessions: {arm['details']['sessions']}, states: {arm['details']['states']}"
            ]
        if arm.get("kvc_delta"):
            lines += ["- Motor KVC delta: " + ", ".join(f"{k}={v:g}" for k, v in arm["kvc_delta"].items())]
        lines += [""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--on-run", type=Path, default=None)
    parser.add_argument("--off-run", type=Path, default=None)
    parser.add_argument("--motor-before", type=Path, default=None)
    parser.add_argument("--motor-after", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("outputs/agent-hint-ci/report"))
    parser.add_argument("--title", default="Agent Hint ON/OFF Performance Report")
    args = parser.parse_args()
    if not args.on_run and not args.off_run:
        parser.error("at least one of --on-run/--off-run is required")

    on = _arm(args.on_run, args.motor_before, args.motor_after) if args.on_run else {}
    off = _arm(args.off_run, args.motor_before, args.motor_after) if args.off_run else {}
    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    markdown = _render_markdown(args.title, on, off, generated)
    report = {"title": args.title, "generated": generated, "on": on, "off": off}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(markdown, encoding="utf-8")
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(markdown)
    print(f"report: {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
