#!/usr/bin/env python3
"""06 复算指标（只读 evidence，不打流）。

口径（与主文档 §12 一致）：
  p50        sorted(ttft_ms)[round(0.5*(n-1))]，n = infer 请求数（nearest-rank）
  mean       全体 infer 的算术平均
  le400/500  ttft <= 阈值 的请求数
  cold p50   call_index == 1 的子集
  resume p50 call_index >= 2 的子集
  Δhits      vllm.prefix_cache_hits_total 前后差（原始差，含 warmup）
  Δqueries   vllm.prefix_cache_queries_total 前后差
  Δprefill   vllm.request_prefill_kv_computed_tokens_sum 前后差
  恒等式     Δhits + Δprefill == Δqueries（精确相等）
  hit/pt     Δhits / Σ prompt_tokens（跨机可比，可 >100%）
  cached%    Δhits / Δqueries（只可同机比）
  复用块     (Δhits - 16640) / 128
  省算       1 - Δprefill / 同规模 off 基线（S1 253022 / S4 265289 / S8 545292 / S32 2150457）
"""
import argparse
import glob
import json
import os
import statistics
import sys

WARMUP_HITS = 16640
OFF_BASELINE = {1: 253022, 4: 265289, 8: 545292, 32: 2150457}


def rd(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def med(xs):
    xs = sorted(xs)
    return xs[round(0.5 * (len(xs) - 1))] if xs else None


def analyze(run_dir):
    meta = rd(os.path.join(run_dir, "meta.json"))
    if not meta:
        return None
    rows = []
    p = os.path.join(run_dir, "requests.jsonl")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    infer = [r for r in rows if r.get("kind") == "infer" or r.get("ttft_ms") is not None]
    ttft = [r["ttft_ms"] for r in infer if r.get("ttft_ms") is not None]
    cold = [r["ttft_ms"] for r in infer if r.get("call_index") == 1 and r.get("ttft_ms") is not None]
    res = [r["ttft_ms"] for r in infer if (r.get("call_index") or 0) >= 2 and r.get("ttft_ms") is not None]
    sp = sum(r.get("prompt_tokens") or 0 for r in infer)

    ma = (rd(os.path.join(run_dir, "metrics_after.json")) or {}).get("vllm") or {}
    mb = (rd(os.path.join(run_dir, "metrics_before.json")) or {}).get("vllm") or {}
    have = "prefix_cache_hits_total" in ma and "prefix_cache_hits_total" in mb
    dh = (ma.get("prefix_cache_hits_total", 0) - mb.get("prefix_cache_hits_total", 0)) if have else None
    dq = (ma.get("prefix_cache_queries_total", 0) - mb.get("prefix_cache_queries_total", 0)) if have else None
    dp = (ma.get("request_prefill_kv_computed_tokens_sum", 0)
          - mb.get("request_prefill_kv_computed_tokens_sum", 0)) if have else None

    rm = rd(os.path.join(run_dir, "_run_meta.json")) or {}
    sessions = meta.get("sessions")
    if sessions is None:
        sessions = len({r.get("session_id") for r in infer if r.get("session_id")})

    base = OFF_BASELINE.get(sessions)
    return {
        "run": os.path.basename(run_dir),
        "arm": meta.get("arm"),
        "lane": meta.get("lane"),
        "sessions": sessions,
        "n": len(ttft),
        "p50": med(ttft),
        "mean": statistics.mean(ttft) if ttft else None,
        "le400": sum(1 for t in ttft if t <= 400),
        "le500": sum(1 for t in ttft if t <= 500),
        "cold_p50": med(cold),
        "resume_p50": med(res),
        "dH": dh, "dQ": dq, "dP": dp,
        "identity_ok": (None if not have else (dh + dp == dq)),
        "hit_pt": (100.0 * dh / sp) if (have and sp) else None,
        "cached_pct": (100.0 * dh / dq) if (have and dq) else None,
        "reuse_blocks": ((dh - WARMUP_HITS) / 128) if have else None,
        "saved_pct": (100.0 * (1 - dp / base)) if (have and base) else None,
        "errs": meta.get("http_errors"),
        "selfcheck": meta.get("arm_selfcheck_ok"),
        "hint_sent": meta.get("hint_sent"),
        "span": meta.get("span_s"),
        "reset": (meta.get("prefix_cache_reset") or {}).get("http_status"),
        "pid_stable": meta.get("coordinator_pid_before") == meta.get("coordinator_pid_after"),
        "engine_pid": rm.get("engine_pid"),
        "hint_before": rm.get("hint_traffic_on_engine_before"),
    }


def fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%%.%df" % nd) % v
    return str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="复算 evidence 指标")
    ap.add_argument("paths", nargs="*", help="evidence 目录（缺省用 AH_EVIDENCE_ROOT）")
    ap.add_argument("--root", default=None, help="evidence 根目录")
    ap.add_argument("--glob", default="*", help="在根目录下的匹配（默认 *）")
    ap.add_argument("--sort", default="name", choices=["name", "p50", "arm"])
    ap.add_argument("--json", default=None, help="把结果写成 json 文件")
    args = ap.parse_args(argv)

    root = args.root or os.environ.get("AH_EVIDENCE_ROOT") or \
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
    dirs = list(args.paths)
    if not dirs:
        dirs = sorted(d for d in glob.glob(os.path.join(root, args.glob))
                      if os.path.isfile(os.path.join(d, "meta.json")))
    out = [x for x in (analyze(d) for d in dirs) if x]

    # 省算基线：优先用内置常量；未知规模时，用同 root 下同会话数的 off 轮 Δprefill 中位数
    off_dp = {}
    for x in out:
        if x["arm"] == "off" and x["dP"]:
            off_dp.setdefault(x["sessions"], []).append(x["dP"])
    for x in out:
        if x["saved_pct"] is None and x["dP"] and x["sessions"] in off_dp:
            base = statistics.median(off_dp[x["sessions"]])
            if base:
                x["saved_pct"] = 100.0 * (1 - x["dP"] / base)
                x["baseline_derived"] = True

    if args.sort == "p50":
        out.sort(key=lambda x: (x["p50"] is None, x["p50"]))
    elif args.sort == "arm":
        out.sort(key=lambda x: (x["arm"] or "", x["run"]))
    else:
        out.sort(key=lambda x: x["run"])

    hdr = ("run", "arm", "lane", "S", "n", "p50", "mean", "le400", "le500",
           "cold50", "res50", "dHits", "dPrefill", "ident", "hit/pt", "cached", "reuse", "saved", "errs", "sc", "base")
    widths = (44, 5, 4, 3, 4, 8, 8, 5, 5, 8, 8, 9, 10, 6, 7, 7, 7, 7, 4, 4, 5)
    line = "".join(h.ljust(w) for h, w in zip(hdr, widths))
    print(line)
    print("-" * len(line))
    for x in out:
        vals = [x["run"], fmt(x["arm"]), fmt(x["lane"]), fmt(x["sessions"]), fmt(x["n"]),
                fmt(x["p50"]), fmt(x["mean"]), fmt(x["le400"]), fmt(x["le500"]),
                fmt(x["cold_p50"]), fmt(x["resume_p50"]), fmt(x["dH"], 0), fmt(x["dP"], 0),
                "OK" if x["identity_ok"] else ("--" if x["identity_ok"] is None else "FAIL"),
                fmt(x["hit_pt"]) + ("%" if x["hit_pt"] is not None else ""),
                fmt(x["cached_pct"]) + ("%" if x["cached_pct"] is not None else ""),
                fmt(x["reuse_blocks"], 0),
                fmt(x["saved_pct"]) + ("%" if x["saved_pct"] is not None else ""),
                fmt(x["errs"]), "T" if x["selfcheck"] else "F",
                ("-" if x["hint_before"] is None else ("Y" if not x["hint_before"] else "N"))]
        print("".join(str(v).ljust(w) for v, w in zip(vals, widths)))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
