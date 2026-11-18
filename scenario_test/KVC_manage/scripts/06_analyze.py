#!/usr/bin/env python3
"""Summarize AISBench lpstd result: victim TTFT and attacker load."""
import argparse
import glob
import io
import json
import os
import re
import sqlite3

import numpy as np


def load_db(perf_dir):
    dbs = glob.glob(os.path.join(perf_dir, "db_data", "*.db"))
    if not dbs:
        return {}
    c = sqlite3.connect(dbs[0])
    out = {}
    for i, blob in c.execute("select id, arr_blob from numpy_store"):
        out[i] = np.load(io.BytesIO(blob))
    c.close()
    return out


def pct(v, p):
    v = sorted(v)
    if not v:
        return None
    return v[round((len(v) - 1) * p)]


def parse_prom(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, errors="ignore"):
        m = re.match(r"^(vllm:[a-zA-Z0-9_]+(?:\{[^}]*\})?)\s+([0-9.eE+-]+)$", line.strip())
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def prefix_delta(result_dir):
    b = parse_prom(os.path.join(result_dir, "engine-before.prom"))
    a = parse_prom(os.path.join(result_dir, "engine-after.prom"))
    keys = {
        "queries": 'vllm:prefix_cache_queries_total{engine="0",model_name="kvh"}',
        "hits": 'vllm:prefix_cache_hits_total{engine="0",model_name="kvh"}',
        "prefill": 'vllm:request_prefill_kv_computed_tokens_sum{engine="0",model_name="kvh"}',
    }
    out = {}
    for name, key in keys.items():
        out[name] = a.get(key, 0) - b.get(key, 0)
    out["hit_rate"] = out["hits"] / out["queries"] if out["queries"] else None
    out["prefill_saved"] = 1 - out["prefill"] / out["queries"] if out["queries"] else None
    return out


def engine_hits(path, session):
    """First cached_before per request for a session (engine SAM log)."""
    if not path or not os.path.exists(path):
        return []
    pat = re.compile(
        r"^.*?(\d{2}:\d{2}:\d{2}).*on_blocks_allocated_for_request request=(\S+) "
        r"session=(\S+).*cached_before=\((\d+),\)"
    )
    seen = {}
    for line in open(path, errors="ignore"):
        m = pat.search(line)
        if not m:
            continue
        t, rid, sid, cached = m.groups()
        if sid != session or rid in seen:
            continue
        seen[rid] = (t, int(cached))
    return [{"time": t, "cached_before": c} for t, c in seen.values()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    ap.add_argument("--session", default="lpstd_victim_00")
    ap.add_argument("--engine-log", default="")
    args = ap.parse_args()
    files = glob.glob(os.path.join(args.result_dir, "**", "performances", "*", "*_details.jsonl"), recursive=True)
    if not files:
        raise SystemExit("details not found")
    details = [json.loads(l) for l in open(files[0]) if l.strip()]
    perf_dir = os.path.dirname(files[0])
    db = load_db(perf_dir)
    rows = []
    for r in details:
        ref = (r.get("time_points") or {}).get("__db_ref__")
        tp = db.get(ref)
        if tp is None or len(tp) < 2:
            continue
        rows.append({
            "session": r.get("session_id"),
            "input": r.get("input"),
            "state": (r.get("extra_perf_data") or {}).get("lifecycle_state"),
            "call": (r.get("extra_perf_data") or {}).get("call_index"),
            "ttft_ms": round((tp[1] - tp[0]) * 1000, 1),
            "e2e_ms": round((tp[-1] - tp[0]) * 1000, 1),
            "in_tok": r.get("input_tokens"),
            "ok": r.get("success"),
        })
    victim = [r for r in rows if r["session"] == args.session]
    attackers = [r for r in rows if r["session"] != args.session]
    print("result", args.result_dir)
    print("details", len(details), "rows", len(rows))
    print("victim:")
    for r in victim:
        print(" ", json.dumps(r, ensure_ascii=False))
    print("attackers n=%d ttft_p50=%.1f mean=%.1f" % (
        len(attackers), pct([r["ttft_ms"] for r in attackers], 0.5),
        sum(r["ttft_ms"] for r in attackers) / len(attackers) if attackers else 0))
    all_ttft = [r["ttft_ms"] for r in rows]
    print("overall n=%d ttft_p50=%.1f mean=%.1f p90=%.1f" % (
        len(all_ttft), pct(all_ttft, 0.5),
        sum(all_ttft) / len(all_ttft) if all_ttft else 0, pct(all_ttft, 0.9)))
    if victim:
        print("victim resume first 3 ttft", [r["ttft_ms"] for r in victim if r["call"] >= 2][:3])
    print("prefix_delta", json.dumps(prefix_delta(args.result_dir), ensure_ascii=False))
    if args.engine_log:
        hits = engine_hits(args.engine_log, args.session)
        print("victim_cached_before", json.dumps(hits, ensure_ascii=False))


if __name__ == "__main__":
    main()
