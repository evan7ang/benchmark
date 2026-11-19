#!/usr/bin/env python3
"""report_arms.py -- per-arm table and cc-vs-baseline summary for the
cache_control A/B on real agent-session traces.

Reads run dirs produced by tests/agent_hint/scripts/run_arm.sh:
  obs-before.json / obs-after.json  engine /metrics snapshots (engine port)
  pool.csv                         2 s sampler: usage running waiting preempt hits queries cached
  recs.json                         per-request: session call_index state prompt_tokens ttft_ms total_ms

Why the split below matters:
  session ids look like real-v22-s0-v03 (a valuable, cache_control-carrying
  session) and real-v22-s0-c05 (churn, one-shot filler traffic).  Only the
  resume requests of the valuable sessions -- call_index >= 2 -- pay a
  re-prefill when their prefix was evicted, so they are the class the gain must
  show up in.  The churn class never carries cache_control and serves as an
  independent control: if it speeds up too, the effect is system level (the
  queue got shorter), not request level.

Metric caveat worth repeating: vllm:prefix_cache_queries_total is inflated in
the cache_control arm (~3.3x measured) because the protection path re-walks the
block registry per request, so hit *rate* is not comparable across arms.  Use
prompt_tokens_cached_total (printed as cached_tok) and client ttft_ms instead.

usage: report_arms.py [-o out.md] <run-dir> [run-dir ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st

PREF = (
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
    "vllm:prompt_tokens_cached_total",
)
ARM_RE = re.compile(r"v\d+s([A-D])(cc|nocc|off)")
VAL_RE = re.compile(r"-v\d+$")
CHURN_RE = re.compile(r"-c\d+$")


def snap(path: str) -> dict:
    d: dict[str, float] = {}
    if not os.path.exists(path):
        return d
    with open(path) as fh:
        for line in fh:
            for k in PREF:
                if line.startswith(k):
                    d[k] = d.get(k, 0.0) + float(line.rsplit(" ", 1)[1])
    return d


def q(a: list, frac: float) -> float:
    if not a:
        return float("nan")
    a = sorted(a)
    return a[min(len(a) - 1, int(frac * (len(a) - 1)))]


def stats(a: list) -> dict:
    if not a:
        return dict(n=0, mean=float("nan"), p50=float("nan"), p90=float("nan"), max=float("nan"))
    return dict(n=len(a), mean=sum(a) / len(a), p50=st.median(a), p90=q(a, 0.90), max=max(a))


def read_arm(d: str) -> dict:
    a, b = snap(os.path.join(d, "obs-before.json")), snap(os.path.join(d, "obs-after.json"))
    row = dict(
        name=os.path.basename(d.rstrip("/\\")),
        queries=b.get(PREF[0], 0) - a.get(PREF[0], 0),
        hits=b.get(PREF[1], 0) - a.get(PREF[1], 0),
        cached_tok=b.get(PREF[2], 0) - a.get(PREF[2], 0),
        usage_mean=float("nan"),
        usage_max=float("nan"),
        waiting_max=float("nan"),
        preempt=float("nan"),
    )
    m = ARM_RE.search(row["name"])
    row["lane"] = m.group(1) if m else "?"
    row["arm"] = m.group(2) if m else "?"

    usage, wait, pre = [], [], 0.0
    pc = os.path.join(d, "pool.csv")
    if os.path.exists(pc):
        with open(pc) as fh:
            for i, line in enumerate(fh):
                if i == 0:
                    continue
                f = line.split()
                if len(f) != 8:
                    continue
                usage.append(float(f[1]))
                wait.append(float(f[3]))
                pre = max(pre, float(f[4]))
    if usage:
        row["usage_mean"] = 100.0 * sum(usage) / len(usage)
        row["usage_max"] = 100.0 * max(usage)
        row["waiting_max"] = max(wait)
        row["preempt"] = pre

    recs = []
    rp = os.path.join(d, "recs.json")
    if os.path.exists(rp):
        try:
            with open(rp) as fh:
                recs = json.load(fh)
        except Exception:
            recs = []
    res, first, churn, tot = [], [], [], []
    for r in recs:
        sid = str(r.get("session") or "")
        ttft = r.get("ttft_ms")
        if ttft is not None and VAL_RE.search(sid):
            (res if int(r.get("call_index") or 0) >= 2 else first).append(ttft)
        if ttft is not None and CHURN_RE.search(sid):
            churn.append(ttft)
        if r.get("total_ms") is not None:
            tot.append(r["total_ms"])
    row["resume"] = stats(res)
    row["first"] = stats(first)
    row["churn"] = stats(churn)
    row["total"] = stats(tot)
    row["rows"] = len(recs)
    row["ok"] = sum(1 for r in recs if r.get("success"))
    return row


def fmt(x: float, nd: int = 0) -> str:
    return "n/a" if x != x else ("%%.%df" % nd) % x


def row_line(r: dict) -> str:
    return "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
        r["lane"], r["arm"], fmt(r["cached_tok"]),
        fmt(r["resume"]["mean"]), fmt(r["resume"]["p50"]), fmt(r["resume"]["p90"]),
        fmt(r["first"]["mean"]), fmt(r["churn"]["mean"]),
        "%s / %s" % (fmt(r["total"]["mean"]), fmt(r["total"]["p50"])),
        "%s / %s" % (fmt(r["usage_mean"], 1), fmt(r["usage_max"], 1)),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="cc / nocc / off arm report")
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("-o", "--out", default="")
    args = ap.parse_args()

    rows = [read_arm(d) for d in args.dirs]
    rows.sort(key=lambda r: (r["lane"], {"off": 0, "nocc": 1, "cc": 2}.get(r["arm"], 3)))

    out = []
    out.append("| lane | arm | cached_tok | resume mean | resume p50 | resume p90 | first mean | churn mean | total mean / p50 | usage mean / max |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        out.append(row_line(r))

    # same lane, same round: cc versus the mean of off/nocc -- never compare
    # across lanes, the shards differ (~8 % in cached tokens) and instances drift.
    out.append("")
    out.append("| lane | baseline mean (off/nocc) | cc | delta | cached_tok gain |")
    out.append("|---|---|---|---|---|")
    tot_base, tot_cc, ct_base, ct_cc = [], [], [], []
    for lane in sorted({r["lane"] for r in rows}):
        base = [r for r in rows if r["lane"] == lane and r["arm"] in ("off", "nocc") and r["total"]["n"]]
        cc = [r for r in rows if r["lane"] == lane and r["arm"] == "cc" and r["total"]["n"]]
        if not base or not cc:
            continue
        b = sum(r["total"]["mean"] for r in base) / len(base)
        c = cc[0]["total"]["mean"]
        cb = sum(r["cached_tok"] for r in base) / len(base)
        ccc = cc[0]["cached_tok"]
        out.append("| %s | %s ms | %s ms | %s%% | %s%% |" % (
            lane, fmt(b), fmt(c), fmt(100.0 * (c - b) / b, 1),
            fmt(100.0 * (ccc - cb) / cb, 1) if cb else "n/a"))
        tot_base += [r["total"]["mean"] for r in base]
        tot_cc += [c]
        ct_base += [cb]
        ct_cc += [ccc]
    if tot_base and tot_cc:
        b, c = sum(tot_base) / len(tot_base), sum(tot_cc) / len(tot_cc)
        cb, ccc = sum(ct_base) / len(ct_base), sum(ct_cc) / len(ct_cc)
        out.append("| **all** | %s ms | %s ms | **%s%%** | **%s%%** |" % (
            fmt(b), fmt(c), fmt(100.0 * (c - b) / b, 1),
            fmt(100.0 * (ccc - cb) / cb, 1) if cb else "n/a"))
    text = "\n".join(out)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    main()