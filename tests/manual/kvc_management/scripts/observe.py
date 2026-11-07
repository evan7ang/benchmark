#!/usr/bin/env python3
"""Live view of KV pressure and KVC counters for manual observation."""

from __future__ import annotations

import argparse
import os
import re
import time
import urllib.request


def scrape(url: str, timeout: float = 2.0) -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception:
        return values
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name = parts[0].split("{", 1)[0]
        try:
            values[name] = values.get(name, 0.0) + float(parts[1])
        except ValueError:
            continue
    return values


def last_kvc_event(log_path: str) -> str:
    if not log_path or not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 20000))
            text = handle.read().decode("utf-8", "replace")
    except Exception:
        return ""
    lines = [line for line in text.splitlines() if "kvc manage dispatch" in line or "kvc " in line and " done:" in line]
    if not lines:
        return ""
    line = lines[-1]
    line = re.sub(r"^.*?(kvc )", r"\1", line)
    return line.strip()[:120]


def main() -> None:
    parser = argparse.ArgumentParser()
    host = os.environ.get("HOST_IP", "127.0.0.1")
    parser.add_argument("--engine-url", default=f"http://{host}:{os.environ.get('ENGINE_PORT', '18000')}/metrics")
    parser.add_argument("--motor-url", default=f"http://{host}:{os.environ.get('MOTOR_OBS_PORT', '19127')}/metrics")
    parser.add_argument("--coord-log", default=os.environ.get("COORD_LOG", ""))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    header = (
        "时间       KV usage  running waiting preempt  offload prefetch evict  "
        "edit_fail send_fail gate_skip"
    )
    if not args.once:
        print(header)
    while True:
        engine = scrape(args.engine_url)
        motor = scrape(args.motor_url)
        kv = engine.get("vllm:kv_cache_usage_perc", 0.0) * 100
        running = int(engine.get("vllm:num_requests_running", 0))
        waiting = int(engine.get("vllm:num_requests_waiting", 0))
        preempt = int(engine.get("vllm:num_preemptions_total", 0))
        offload = int(motor.get("motor:kvc_offload_total", 0))
        prefetch = int(motor.get("motor:kvc_prefetch_total", 0))
        evict = int(motor.get("motor:kvc_evict_total", 0))
        edit_fail = int(motor.get("motor:kvc_edit_fail_total", 0))
        send_fail = int(motor.get("motor:kvc_send_fail_total", 0))
        gate_skip = int(motor.get("motor:kvc_gate_skip_total", 0))
        line = (
            f"{time.strftime('%H:%M:%S')}  {kv:7.1f}%  {running:7d} {waiting:7d} "
            f"{preempt:7d}  {offload:7d} {prefetch:8d} {evict:5d}  "
            f"{edit_fail:9d} {send_fail:9d} {gate_skip:9d}"
        )
        if args.once:
            print(line)
            event = last_kvc_event(args.coord_log)
            if event:
                print("最近KVC:", event)
            return
        print(line)
        event = last_kvc_event(args.coord_log)
        if event:
            print("          最近KVC:", event)
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    main()
