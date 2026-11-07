#!/usr/bin/env python3
"""Build small, complete-session KVC functional-test datasets.

The source files were already tokenizer-validated by the full builder
(``build_agent_dataset_v2_pause.py``). This script only *selects* whole
sessions, preserving their internal order, so prompt/context validity is
inherited from the audited source.

Modes:
  pause     -> sessions containing pause -> resume (offload/prefetch)
  finalize  -> sessions ending with finalize (evict)
  gate      -> exactly one short session with one pause (gate/boundary)
  all       -> build all three
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import OrderedDict
from typing import Any


def state_of(row: dict[str, Any]) -> str | None:
    status = (row.get("agent_hint") or {}).get("agent_status") or {}
    value = status.get("state")
    return value.lower() if isinstance(value, str) else None


def session_of(row: dict[str, Any]) -> str:
    session = (row.get("agent_hint") or {}).get("session") or {}
    return str(session.get("session_id") or "")


def load_rows(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def group_sessions(rows: list[dict[str, Any]]) -> "OrderedDict[str, list[dict[str, Any]]]":
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(session_of(row), []).append(row)
    return grouped


def pause_points(items: list[dict[str, Any]]) -> int:
    return sum(1 for item in items if state_of(item) == "pause")


def has_pause_resume(items: list[dict[str, Any]]) -> bool:
    states = [state_of(item) for item in items]
    for idx, value in enumerate(states):
        if value in ("pause", "suspend", "tool"):
            for nxt in states[idx + 1 :]:
                if nxt in ("resume", "running"):
                    return True
                break
    return False


def ends_finalize(items: list[dict[str, Any]]) -> bool:
    return bool(items) and state_of(items[-1]) == "finalize"


def select_sessions(
    grouped: "OrderedDict[str, list[dict[str, Any]]]",
    *,
    target_rows: int,
    target_pauses: int,
    target_sessions: int,
    require_finalize: bool,
    require_pause: bool,
    min_rows: int,
    max_sessions: int | None = None,
) -> list[str]:
    candidates: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for order, (sid, items) in enumerate(grouped.items()):
        if not sid or len(items) < min_rows:
            continue
        if require_finalize and not ends_finalize(items):
            continue
        if require_pause and not has_pause_resume(items):
            continue
        candidates.append((len(items), order, sid, items))
    # Prefer shorter sessions so a small dataset still exercises many
    # concurrent session workers (needed to fill KV).
    candidates.sort(key=lambda item: (item[0], item[1]))

    chosen: list[str] = []
    rows_total = 0
    pauses_total = 0
    for _, _, sid, items in candidates:
        if max_sessions is not None and len(chosen) >= max_sessions:
            break
        if (
            chosen
            and rows_total >= target_rows
            and pauses_total >= target_pauses
            and len(chosen) >= target_sessions
        ):
            break
        chosen.append(sid)
        rows_total += len(items)
        pauses_total += pause_points(items)
    return chosen


def materialize(
    rows: list[dict[str, Any]],
    selected: set[str],
    *,
    delay_ms: int | None,
    max_out_cap: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if session_of(row) not in selected:
            continue
        row = json.loads(json.dumps(row))
        value = state_of(row)
        if value in ("pause", "suspend", "tool"):
            if delay_ms is not None:
                row["delay_after_ms"] = max(int(row.get("delay_after_ms") or 0), delay_ms)
        else:
            row.setdefault("delay_after_ms", None)
        if max_out_cap > 0 and row.get("max_out_len") is not None:
            row["max_out_len"] = max(1, min(int(row["max_out_len"]), max_out_cap))
        out.append(row)
    return out


def summarize(out: list[dict[str, Any]], source: str, mode: str) -> dict[str, Any]:
    grouped = group_sessions(out)
    states: dict[str, int] = {}
    for row in out:
        key = state_of(row) or "null"
        states[key] = states.get(key, 0) + 1
    delays = [int(r.get("delay_after_ms") or 0) for r in out if state_of(r) in ("pause", "suspend", "tool")]
    session_sizes = [len(v) for v in grouped.values()]
    checks = {
        "pause_followed_by_resume": True,
        "finalize_only_last": True,
        "delay_gt_5s": True,
    }
    for items in grouped.values():
        st = [state_of(i) for i in items]
        for idx, value in enumerate(st):
            if value in ("pause", "suspend", "tool"):
                nxt = st[idx + 1] if idx + 1 < len(st) else None
                if nxt not in ("resume", "running"):
                    checks["pause_followed_by_resume"] = False
        if "finalize" in st[:-1]:
            checks["finalize_only_last"] = False
    if delays and min(delays) <= 5000:
        checks["delay_gt_5s"] = False
    return {
        "mode": mode,
        "source": source,
        "rows": len(out),
        "sessions": len(grouped),
        "states": states,
        "pause_points": states.get("pause", 0),
        "resume_points": states.get("resume", 0),
        "finalize_points": states.get("finalize", 0),
        "delay_after_ms": {
            "min": min(delays) if delays else None,
            "median": statistics.median(delays) if delays else None,
            "max": max(delays) if delays else None,
        },
        "session_rows": {
            "min": min(session_sizes) if session_sizes else 0,
            "median": statistics.median(session_sizes) if session_sizes else 0,
            "max": max(session_sizes) if session_sizes else 0,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }


def build_one(
    *,
    mode: str,
    source: str,
    output: str,
    target_rows: int,
    target_pauses: int,
    target_sessions: int,
    require_finalize: bool,
    require_pause: bool,
    min_rows: int,
    delay_ms: int | None,
    max_sessions: int | None,
    max_out_cap: int = 0,
) -> dict[str, Any]:
    rows = load_rows(source)
    grouped = group_sessions(rows)
    selected = select_sessions(
        grouped,
        target_rows=target_rows,
        target_pauses=target_pauses,
        target_sessions=target_sessions,
        require_finalize=require_finalize,
        require_pause=require_pause,
        min_rows=min_rows,
        max_sessions=max_sessions,
    )
    if not selected:
        raise SystemExit(f"no eligible sessions for mode={mode} in {source}")
    out = materialize(rows, set(selected), delay_ms=delay_ms, max_out_cap=max_out_cap)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        for row in out:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = summarize(out, source, mode)
    report["selected_sessions"] = selected
    report["output"] = output
    with open(output + ".report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(
        f"[{mode}] rows={report['rows']} sessions={report['sessions']} "
        f"pause={report['pause_points']} resume={report['resume_points']} "
        f"finalize={report['finalize_points']} checks={report['all_checks_passed']} -> {output}"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["pause", "pause-functional", "finalize", "gate", "all"],
        default="all",
    )
    source_dir = os.environ.get("SOURCE_DATA_DIR", "")
    data_dir = os.environ.get("DATA_DIR", ".")
    parser.add_argument(
        "--input-finalize",
        default=(os.path.join(source_dir, "swebench_full50_v2_finalize.jsonl") if source_dir else ""),
    )
    parser.add_argument(
        "--input-pause",
        default=(os.path.join(source_dir, "swebench_full50_v2_pause_offload.jsonl") if source_dir else ""),
    )
    parser.add_argument("--out-dir", default=data_dir)
    parser.add_argument("--pause-target-rows", type=int, default=48)
    parser.add_argument("--pause-target-pauses", type=int, default=10)
    parser.add_argument("--pause-target-sessions", type=int, default=8)
    parser.add_argument("--pause-functional-rows", type=int, default=24)
    parser.add_argument("--pause-functional-pauses", type=int, default=4)
    parser.add_argument("--pause-functional-sessions", type=int, default=4)
    parser.add_argument("--finalize-target-rows", type=int, default=30)
    parser.add_argument("--finalize-target-sessions", type=int, default=8)
    parser.add_argument("--delay-ms", type=int, default=45000)
    parser.add_argument("--tag", default="", help="suffix inserted before .jsonl, e.g. _fast")
    parser.add_argument("--max-out-cap", type=int, default=0)
    parser.add_argument("--gate-max-out-cap", type=int, default=0)
    args = parser.parse_args()

    modes = ["pause-functional", "finalize", "gate"] if args.mode == "all" else [args.mode]
    for mode in modes:
        if mode == "pause-functional":
            build_one(
                mode=mode,
                source=args.input_pause,
                output=os.path.join(args.out_dir, f"kvc_manual_pause_offload_functional{args.tag}.jsonl"),
                target_rows=args.pause_functional_rows,
                target_pauses=args.pause_functional_pauses,
                target_sessions=args.pause_functional_sessions,
                require_finalize=False,
                require_pause=True,
                min_rows=5,
                delay_ms=args.delay_ms,
                max_sessions=None,
                max_out_cap=args.max_out_cap,
            )
        elif mode == "pause":
            build_one(
                mode=mode,
                source=args.input_pause,
                output=os.path.join(args.out_dir, f"kvc_manual_pause_offload_quick{args.tag}.jsonl"),
                target_rows=args.pause_target_rows,
                target_pauses=args.pause_target_pauses,
                target_sessions=args.pause_target_sessions,
                require_finalize=False,
                require_pause=True,
                min_rows=5,
                delay_ms=args.delay_ms,
                max_sessions=None,
                max_out_cap=args.max_out_cap,
            )
        elif mode == "finalize":
            build_one(
                mode=mode,
                source=args.input_finalize,
                output=os.path.join(args.out_dir, f"kvc_manual_finalize_quick{args.tag}.jsonl"),
                target_rows=args.finalize_target_rows,
                target_pauses=0,
                target_sessions=args.finalize_target_sessions,
                require_finalize=True,
                require_pause=False,
                min_rows=4,
                delay_ms=None,
                max_sessions=None,
                max_out_cap=args.max_out_cap,
            )
        else:  # gate
            build_one(
                mode=mode,
                source=args.input_pause,
                output=os.path.join(args.out_dir, f"kvc_manual_gate_quick{args.tag}.jsonl"),
                target_rows=8,
                target_pauses=1,
                target_sessions=1,
                require_finalize=True,
                require_pause=True,
                min_rows=5,
                delay_ms=args.delay_ms,
                max_sessions=1,
                max_out_cap=args.gate_max_out_cap,
            )


if __name__ == "__main__":
    main()
