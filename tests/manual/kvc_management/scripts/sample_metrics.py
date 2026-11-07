#!/usr/bin/env python3
"""Sample engine and Motor metrics once per second into a CSV."""

from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
import urllib.request

STOP = False


def _stop(_signum, _frame):
    global STOP
    STOP = True


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
        name_part, raw_value = parts
        name = name_part.split("{", 1)[0]
        try:
            value = float(raw_value)
        except ValueError:
            continue
        values[name] = values.get(name, 0.0) + value
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    host = os.environ.get("HOST_IP", "127.0.0.1")
    parser.add_argument("--engine-url", default=f"http://{host}:{os.environ.get('ENGINE_PORT', '18000')}/metrics")
    parser.add_argument("--motor-url", default=f"http://{host}:{os.environ.get('MOTOR_OBS_PORT', '19127')}/metrics")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    engine_keys = [
        "vllm:kv_cache_usage_perc",
        "vllm:num_requests_waiting",
        "vllm:num_requests_running",
        "vllm:num_preemptions_total",
        "vllm:prefix_cache_hits_total",
        "vllm:prefix_cache_queries_total",
    ]
    motor_keys = [
        "motor:kvc_offload_total",
        "motor:kvc_prefetch_total",
        "motor:kvc_evict_total",
        "motor:kvc_edit_success_total",
        "motor:kvc_edit_fail_total",
        "motor:kvc_send_fail_total",
        "motor:kvc_gate_skip_total",
        "motor:kvc_queue_full_total",
    ]
    fields = ["ts", "date"] + [f"engine_{k.split(':')[-1]}" for k in engine_keys] + [
        f"motor_{k.split(':')[-1]}" for k in motor_keys
    ]

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        handle.flush()
        while not STOP:
            now = time.time()
            engine = scrape(args.engine_url)
            motor = scrape(args.motor_url)
            row: dict[str, object] = {"ts": now, "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}
            for key in engine_keys:
                row[f"engine_{key.split(':')[-1]}"] = engine.get(key, "")
            for key in motor_keys:
                row[f"motor_{key.split(':')[-1]}"] = motor.get(key, "")
            writer.writerow(row)
            handle.flush()
            time.sleep(max(0.1, args.interval))
    print("sampler stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
