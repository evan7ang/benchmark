#!/usr/bin/env python3
"""Analyze TTFT Issue #52113 arms from AISBench details + engine metrics."""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import sqlite3
import statistics
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--arm", action="append", required=True, help="name=path (repeatable)")
    p.add_argument("--baseline", default="off")
    p.add_argument("--compare", default="on")
    p.add_argument("--dataset", default=None, help="Dataset used for data_id -> session/turn mapping")
    p.add_argument("--out", required=True)
    return p.parse_args()


def resolve_workdir(path: str) -> Path:
    p = Path(path)
    if (p / "performances").is_dir():
        return p
    if (p / "aisbench").is_dir():
        candidates = [d for d in (p / "aisbench").iterdir() if (d / "performances").is_dir()]
        if candidates:
            return sorted(candidates)[-1]
    raise SystemExit(f"cannot resolve AISBench work dir: {path}")


def find_metrics(path: str) -> Path | None:
    p = Path(path)
    for parent in [p, *p.parents]:
        candidate = parent / "engine-metrics.csv"
        if candidate.exists():
            return candidate
        if parent.name == "results":
            break
    return None


def load_dataset_map(path: str) -> dict[int, tuple[str, int]]:
    mapping: dict[int, tuple[str, int]] = {}
    counts: dict[str, int] = {}
    for index, line in enumerate(open(path, encoding="utf-8-sig")):
        if not line.strip():
            continue
        row = json.loads(line)
        hint = row.get("agent_hint") or {}
        nested = hint.get("session") or {}
        sid = str(nested.get("session_id") or hint.get("session_id") or "")
        turn = counts.get(sid, 0)
        counts[sid] = turn + 1
        mapping[index] = (sid, turn)
    return mapping


def load_arm(path: str, dataset_map: dict[int, tuple[str, int]] | None = None) -> tuple[list[dict[str, Any]], str]:
    workdir = resolve_workdir(path)
    details = glob.glob(os.path.join(workdir, "performances", "*", "*_details.jsonl"))
    if not details:
        raise SystemExit(f"no details in {workdir}")
    db_files = glob.glob(os.path.join(workdir, "performances", "*", "db_data", "*.db"))
    arrays: dict[Any, Any] = {}
    if db_files:
        con = sqlite3.connect(db_files[0])
        try:
            for idx, blob in con.execute("select id, arr_blob from numpy_store"):
                arrays[idx] = np.load(io.BytesIO(blob), allow_pickle=False)
        finally:
            con.close()

    rows: list[dict[str, Any]] = []
    for detail_path in details:
        with open(detail_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if not raw.get("success"):
                    continue
                tp = raw.get("time_points")
                if isinstance(tp, dict) and "__db_ref__" in tp:
                    tp = arrays.get(tp["__db_ref__"])
                if tp is None or len(tp) < 2:
                    continue
                ttft = float(tp[1] - tp[0]) * 1000.0
                e2el = float(tp[-1] - tp[0]) * 1000.0
                output_tokens = int(raw.get("output_tokens") or 0)
                tpot = (e2el - ttft) / max(output_tokens - 1, 1)
                extra = raw.get("extra_perf_data") or {}
                data_id = raw.get("data_id", raw.get("id"))
                session_id = raw.get("session_id") or ""
                turn_id = raw.get("turn_id")
                if dataset_map is not None and data_id in dataset_map:
                    session_id, turn_id = dataset_map[data_id]
                rows.append(
                    {
                        "data_id": data_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "ttft_ms": ttft,
                        "e2el_ms": e2el,
                        "tpot_ms": tpot,
                        "input_tokens": int(raw.get("input_tokens") or 0),
                        "output_tokens": output_tokens,
                        "lifecycle_state": extra.get("lifecycle_state", ""),
                        "cache_control_ttl_s": extra.get("cache_control_ttl_s"),
                    }
                )
    rows.sort(key=lambda r: (r["data_id"] is None, r["data_id"]))
    return rows, str(workdir)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": pct(values, 50),
        "p90": pct(values, 90),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "mean": statistics.mean(values),
        "min": min(values),
        "max": max(values),
    }


def bucket(tokens: int) -> str:
    if tokens < 10000:
        return "<10k"
    if tokens < 20000:
        return "10-20k"
    if tokens < 25000:
        return "20-25k"
    return ">=25k"


def metric_stats(path: str | None) -> dict[str, Any]:
    if not path or not os.path.exists(path):
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

    def summary(key: str) -> dict[str, Any]:
        values = columns.get(key, [])
        if not values:
            return {}
        return {
            "p50": pct(values, 50),
            "p90": pct(values, 90),
            "max": max(values),
            "last": values[-1],
            "delta": values[-1] - values[0],
        }

    result = {
        "kv_cache_usage_perc": summary("engine_kv_cache_usage_perc"),
        "num_requests_waiting": summary("engine_num_requests_waiting"),
        "num_requests_running": summary("engine_num_requests_running"),
        "num_preemptions_total": summary("engine_num_preemptions_total"),
        "prefix_cache_hits_total": summary("engine_prefix_cache_hits_total"),
        "prefix_cache_queries_total": summary("engine_prefix_cache_queries_total"),
        "motor_evict_total": summary("motor_kvc_evict_total"),
        "motor_edit_fail_total": summary("motor_kvc_edit_fail_total"),
        "motor_gate_skip_total": summary("motor_kvc_gate_skip_total"),
    }
    hits = result["prefix_cache_hits_total"].get("last")
    queries = result["prefix_cache_queries_total"].get("last")
    result["prefix_cache_hit_rate"] = hits / queries if hits and queries else None
    return result


def arm_summary(rows: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    by_bucket: dict[str, list[float]] = {}
    for row in rows:
        by_bucket.setdefault(bucket(row["input_tokens"]), []).append(row["ttft_ms"])
    first = [r["ttft_ms"] for r in rows if r["turn_id"] == 0]
    later = [r["ttft_ms"] for r in rows if (r["turn_id"] or 0) > 0]
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(row["session_id"], []).append(row)
    session_stats = {
        sid: {
            "n": len(items),
            "ttft": stats([r["ttft_ms"] for r in items]),
            "first": stats([r["ttft_ms"] for r in items if r["turn_id"] == 0]),
            "later": stats([r["ttft_ms"] for r in items if (r["turn_id"] or 0) > 0]),
        }
        for sid, items in by_session.items()
    }
    return {
        "n": len(rows),
        "sessions": len(by_session),
        "ttft": stats([r["ttft_ms"] for r in rows]),
        "e2el": stats([r["e2el_ms"] for r in rows]),
        "tpot": stats([r["tpot_ms"] for r in rows]),
        "input_tokens": stats([r["input_tokens"] for r in rows]),
        "output_tokens": stats([r["output_tokens"] for r in rows]),
        "first_turn": stats(first),
        "later_turn": stats(later),
        "buckets": {key: stats(values) for key, values in sorted(by_bucket.items())},
        "sessions_detail": session_stats,
        "metrics": metrics,
    }


def paired(baseline: list[dict[str, Any]], compare: list[dict[str, Any]]) -> dict[str, Any]:
    b = {r["data_id"]: r for r in baseline}
    c = {r["data_id"]: r for r in compare}
    common = sorted(set(b) & set(c), key=lambda x: (x is None, x))
    ratios = [c[i]["ttft_ms"] / b[i]["ttft_ms"] for i in common if b[i]["ttft_ms"] > 0]
    later = [c[i]["ttft_ms"] / b[i]["ttft_ms"] for i in common if b[i]["ttft_ms"] > 0 and (b[i]["turn_id"] or 0) > 0]
    first = [c[i]["ttft_ms"] / b[i]["ttft_ms"] for i in common if b[i]["ttft_ms"] > 0 and b[i]["turn_id"] == 0]
    return {
        "common": len(common),
        "median": statistics.median(ratios) if ratios else None,
        "mean": statistics.mean(ratios) if ratios else None,
        "p25": pct(ratios, 25),
        "p75": pct(ratios, 75),
        "improved": sum(1 for x in ratios if x < 1.0),
        "degraded": sum(1 for x in ratios if x > 1.0),
        "first_turn_median": statistics.median(first) if first else None,
        "later_turn_median": statistics.median(later) if later else None,
    }


def ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b is not None and b != 0 else None


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> None:
    args = parse_args()
    dataset_map = load_dataset_map(args.dataset) if args.dataset else None
    arms: dict[str, dict[str, Any]] = {}
    for spec in args.arm:
        if "=" not in spec:
            raise SystemExit(f"--arm expects name=path, got {spec!r}")
        name, path = spec.split("=", 1)
        rows, workdir = load_arm(path, dataset_map)
        metrics = metric_stats(str(find_metrics(path)) if find_metrics(path) else None)
        arms[name] = {"path": path, "workdir": workdir, "rows": rows, **arm_summary(rows, metrics)}

    baseline = arms.get(args.baseline)
    compare = arms.get(args.compare)
    paired_result = {}
    if baseline and compare:
        paired_result = paired(baseline["rows"], compare["rows"])

    report = {
        "arms": {name: {k: v for k, v in arm.items() if k != "rows"} for name, arm in arms.items()},
        "baseline": args.baseline,
        "compare": args.compare,
        "paired": paired_result,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "analysis.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    lines = ["# TTFT Issue #52113 分析", ""]
    lines.append("| arm | n | TTFT P50 | P95 | P99 | mean | first P50 | later P50 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, arm in arms.items():
        t = arm["ttft"]
        lines.append(
            f"| {name} | {arm['n']} | {fmt(t.get('p50'))} | {fmt(t.get('p95'))} | "
            f"{fmt(t.get('p99'))} | {fmt(t.get('mean'))} | "
            f"{fmt(arm['first_turn'].get('p50'))} | {fmt(arm['later_turn'].get('p50'))} |"
        )
    lines.append("")
    if baseline and compare:
        lines.append(f"## {args.compare} vs {args.baseline}")
        lines.append("")
        lines.append(
            f"- TTFT P50 ratio: **{fmt(ratio(compare['ttft'].get('p50'), baseline['ttft'].get('p50')), 3)}**"
        )
        lines.append(
            f"- TTFT P95 ratio: {fmt(ratio(compare['ttft'].get('p95'), baseline['ttft'].get('p95')), 3)}"
        )
        lines.append(
            f"- TTFT P99 ratio: {fmt(ratio(compare['ttft'].get('p99'), baseline['ttft'].get('p99')), 3)}"
        )
        lines.append(
            f"- first-turn P50 ratio: {fmt(ratio(compare['first_turn'].get('p50'), baseline['first_turn'].get('p50')), 3)}"
        )
        lines.append(
            f"- later-turn P50 ratio: {fmt(ratio(compare['later_turn'].get('p50'), baseline['later_turn'].get('p50')), 3)}"
        )
        lines.append(f"- paired median: {fmt(paired_result.get('median'), 3)}")
        lines.append(
            f"- improved / degraded: {paired_result.get('improved')} / {paired_result.get('degraded')}"
        )
        lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| arm | KV p50 | KV p90 | KV max | Waiting max | Preemptions | prefix hit rate |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, arm in arms.items():
        m = arm.get("metrics") or {}
        kv = m.get("kv_cache_usage_perc") or {}
        wait = m.get("num_requests_waiting") or {}
        pre = m.get("num_preemptions_total") or {}
        hit = m.get("prefix_cache_hit_rate")
        lines.append(
            f"| {name} | {fmt(kv.get('p50'))} | {fmt(kv.get('p90'))} | {fmt(kv.get('max'))} | "
            f"{fmt(wait.get('max'))} | {fmt(pre.get('delta'), 0)} | {fmt(hit, 3)} |"
        )
    with open(os.path.join(args.out, "summary.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
