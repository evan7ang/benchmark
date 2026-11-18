#!/usr/bin/env python3
"""06 复算指标（只读 evidence，不打流）。

口径（与主文档一致；churn 形态见 §分组）：
  p50        agent 组的 sorted(ttft_ms)[round(0.5*(n-1))]（nearest-rank）
  mean       agent 组算术平均
  le400/500  agent 组 ttft <= 阈值 的请求数
  cold p50   agent 组 call_index == 1 的子集
  resume p50 agent 组 call_index >= 2 的子集
  churn p50  churn-* 会话组（压力源健康度，不参与收益判断）
  Δhits      vllm.prefix_cache_hits_total 前后差（原始差，含 warmup）
  Δqueries   vllm.prefix_cache_queries_total 前后差
  Δprefill   vllm.request_prefill_kv_computed_tokens_sum 前后差
  恒等式     Δhits + Δprefill == Δqueries；
             churn 形态允许 |差| <= identity_tolerance_bp（0.25%）× Δqueries（采样边界竞态）
  hit/pt     Δhits / Σ prompt_tokens（跨机可比，可 >100%）
  cached%    Δhits / Δqueries（只可同机比）
  复用块     churn 形态：Δhits / 128（warmup 块可能已被 churn 挤掉）
             非 churn：  (Δhits - 16640) / 128
  省算       1 - Δprefill / 同数据集 off 轮的 Δprefill 中位数（无则退回内置常量，标注来源）
  压力       churn 形态：(Σ churn prompt_tokens / 128) / span_s  块/秒

口径警告（churn 形态）：
  收益结论只看 **agent 组**（agP50 / agMean / le500 / cold / resume）。
  allP50 / allMean 是**全体请求**（含 churn）——churn 占 ~90% 且两臂行为相近，
  会把效应稀释掉（实测 agent −82% 而全体仅 −4.7%），不可用作结论口径。
"""
import argparse
import glob
import json
import os
import statistics
import sys

WARMUP_HITS = 16640
# 兼容旧证据的内置基线（仅非 churn、未知基线时兜底；输出会标注 base=builtin）
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


def is_churn(sid, prefix):
    return str(sid or "").startswith(prefix)


def analyze(run_dir, churn_prefix="churn-", tol_bp=25):
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
    infer = [r for r in rows if r.get("ttft_ms") is not None]
    agent = [r for r in infer if not is_churn(r.get("session_id"), churn_prefix)]
    churn = [r for r in infer if is_churn(r.get("session_id"), churn_prefix)]

    def ttfts(rs):
        return [r["ttft_ms"] for r in rs if r.get("ttft_ms") is not None]

    ag = ttfts(agent)
    ch = ttfts(churn)
    allv = ttfts(infer)
    cold = ttfts([r for r in agent if r.get("call_index") == 1])
    res = ttfts([r for r in agent if (r.get("call_index") or 0) >= 2])
    sp_agent = sum(r.get("prompt_tokens") or 0 for r in agent)
    sp_churn = sum(r.get("prompt_tokens") or 0 for r in churn)

    ma = (rd(os.path.join(run_dir, "metrics_after.json")) or {}).get("vllm") or {}
    mb = (rd(os.path.join(run_dir, "metrics_before.json")) or {}).get("vllm") or {}
    have = "prefix_cache_hits_total" in ma and "prefix_cache_hits_total" in mb
    dh = (ma.get("prefix_cache_hits_total", 0) - mb.get("prefix_cache_hits_total", 0)) if have else None
    dq = (ma.get("prefix_cache_queries_total", 0) - mb.get("prefix_cache_queries_total", 0)) if have else None
    dp = (ma.get("request_prefill_kv_computed_tokens_sum", 0)
          - mb.get("request_prefill_kv_computed_tokens_sum", 0)) if have else None

    rm = rd(os.path.join(run_dir, "_run_meta.json")) or {}
    # 会话数只统计 agent 组（meta.sessions 会把 churn 会话也算进去）
    sessions = len({r.get("session_id") for r in agent})
    has_churn = len(churn) > 0

    # 压力：churn 组 token 总量 / 覆盖时长
    pressure = None
    ch_ts = [r.get("actual_ts_ms") for r in churn if r.get("actual_ts_ms")]
    if has_churn and ch_ts and sp_churn:
        span_s = max(0.001, (max(ch_ts) - min(ch_ts)) / 1000.0)
        pressure = (sp_churn / 128.0) / span_s

    if have:
        if has_churn:
            tol = max(128.0, (tol_bp / 10000.0) * abs(dq or 0))
            identity_delta = (dh + dp) - dq
            identity_ok = abs(identity_delta) <= tol
            reuse = dh / 128.0
        else:
            identity_ok = (dh + dp == dq)
            identity_delta = (dh + dp) - dq
            reuse = (dh - WARMUP_HITS) / 128.0
    else:
        identity_ok = None
        identity_delta = None
        reuse = None

    ds_name = os.path.basename(str(meta.get("dataset") or "")).replace(".jsonl", "")
    return {
        "run": os.path.basename(run_dir),
        "arm": meta.get("arm"),
        "form": rm.get("form"),
        "lane": meta.get("lane"),
        "dataset": ds_name,
        "sessions": sessions,
        "has_churn": has_churn,
        "n": len(ag), "churn_n": len(ch), "all_n": len(allv),
        "p50": med(ag), "mean": statistics.mean(ag) if ag else None,
        "all_p50": med(allv), "all_mean": statistics.mean(allv) if allv else None,
        "le400": sum(1 for t in ag if t <= 400),
        "le500": sum(1 for t in ag if t <= 500),
        "cold_p50": med(cold), "resume_p50": med(res),
        "churn_p50": med(ch),
        "dH": dh, "dQ": dq, "dP": dp,
        "identity_ok": identity_ok, "identity_delta": identity_delta,
        "hit_pt": (100.0 * dh / sp_agent) if (have and sp_agent) else None,
        "cached_pct": (100.0 * dh / dq) if (have and dq) else None,
        "reuse_blocks": reuse,
        "saved_pct": None,          # 由 main 依据同数据集 off 轮填充
        "baseline": None,
        "pressure_blocks_s": pressure,
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
    ap.add_argument("--churn-prefix", default="churn-")
    ap.add_argument("--tol-bp", type=int, default=25, help="churn 恒等式容差（万分之，默认 0.25%）")
    ap.add_argument("--json", default=None, help="把结果写成 json 文件")
    args = ap.parse_args(argv)

    root = args.root or os.environ.get("AH_EVIDENCE_ROOT") or \
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
    dirs = list(args.paths)
    if not dirs:
        dirs = sorted(d for d in glob.glob(os.path.join(root, args.glob))
                      if os.path.isfile(os.path.join(d, "meta.json")))
    out = [x for x in (analyze(d, args.churn_prefix, args.tol_bp) for d in dirs) if x]

    # 省算基线：优先同数据集 off 轮的 Δprefill 中位数；否则退回内置常量
    off_dp = {}
    for x in out:
        if x["arm"] == "off" and x["dP"]:
            off_dp.setdefault(x["dataset"], []).append(x["dP"])
    for x in out:
        if not (x["dP"] and x["arm"] in ("decl", "life")):
            continue
        base, src = None, None
        if off_dp.get(x["dataset"]):
            base, src = statistics.median(off_dp[x["dataset"]]), "off_rounds"
        elif not x["has_churn"]:
            base, src = OFF_BASELINE.get(x["sessions"]), "builtin"
        if base:
            x["saved_pct"] = 100.0 * (1 - x["dP"] / base)
            x["baseline"] = src

    if args.sort == "p50":
        out.sort(key=lambda x: (x["p50"] is None, x["p50"]))
    elif args.sort == "arm":
        out.sort(key=lambda x: (x["arm"] or "", x["run"]))
    else:
        out.sort(key=lambda x: x["run"])

    hdr = ("run", "form", "arm", "lane", "S", "agN", "chN", "agP50", "allP50", "agMean",
           "allMean", "le500", "cold50", "res50", "churn50", "dHits", "dPrefill", "ident",
           "hit/pt", "cached", "reuse", "saved", "press", "errs", "sc", "base")
    widths = (52, 6, 5, 4, 3, 4, 4, 8, 8, 8, 8, 5, 8, 8, 8, 9, 10, 11, 7, 7, 7, 7, 6, 4, 3, 9)
    line = "".join(h.ljust(w) for h, w in zip(hdr, widths))
    print(line)
    print("-" * len(line))
    for x in out:
        ident = "--" if x["identity_ok"] is None else ("OK" if x["identity_ok"] else "FAIL")
        if x["identity_delta"] not in (None, 0):
            ident = "%s(%+d)" % (ident, x["identity_delta"])
        vals = [x["run"], fmt(x["form"]), fmt(x["arm"]), fmt(x["lane"]), fmt(x["sessions"]),
                fmt(x["n"]), fmt(x["churn_n"]), fmt(x["p50"]), fmt(x["all_p50"]),
                fmt(x["mean"]), fmt(x["all_mean"]), fmt(x["le500"]),
                fmt(x["cold_p50"]), fmt(x["resume_p50"]), fmt(x["churn_p50"]),
                fmt(x["dH"], 0), fmt(x["dP"], 0), ident,
                fmt(x["hit_pt"]) + ("%" if x["hit_pt"] is not None else ""),
                fmt(x["cached_pct"]) + ("%" if x["cached_pct"] is not None else ""),
                fmt(x["reuse_blocks"], 0),
                fmt(x["saved_pct"]) + ("%" if x["saved_pct"] is not None else ""),
                fmt(x["pressure_blocks_s"], 1), fmt(x["errs"]),
                "T" if x["selfcheck"] else "F", fmt(x["baseline"])]
        print("".join(str(v).ljust(w) for v, w in zip(vals, widths)))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
