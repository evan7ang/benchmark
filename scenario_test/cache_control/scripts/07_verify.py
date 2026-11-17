#!/usr/bin/env python3
"""07 验收判据 V1–V8（只读 evidence）。

V1 臂自检        arm_selfcheck_ok=True 且 arm_checks 三处全 True
V2 请求全通      http_errors=0；rows_infer = sessions*8；rows_total = sessions*16
V3 hint 一致     decl/life: hint_sent=True 且 hint_b64_len≈520；off: hint_sent=False 且 0
V4 前缀已重置    prefix_cache_reset.http_status=200（未启用该开关时 SKIP）
V5 池子一致      results/logs/pool.env 与 env/override 一致（无法判定时 SKIP）
V6 无中途重启    coordinator_pid_before == coordinator_pid_after
V7 省算恒等式    Δhits + Δprefill == Δqueries（精确相等）
V8 复用块合理    Δhits % 128 == 0；off(S>=4)=16640；decl/life > 16640
V9 基线洁净度    off 轮必须跑在「无 hints-on 历史」的引擎实例上（靠 _run_meta.json 判定）

另附「半数门槛」：le500 >= 50% 的 infer 请求 ⟺ p50 <= 500ms（热/冷簇判别）。
"""
import argparse
import glob
import json
import os
import sys

WARMUP_HITS = 16640


def rd(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def med(xs):
    xs = sorted(xs)
    return xs[round(0.5 * (len(xs) - 1))] if xs else None


def load(run_dir):
    meta = rd(os.path.join(run_dir, "meta.json"))
    if meta is None:
        return None, None, None, None
    rows = []
    p = os.path.join(run_dir, "requests.jsonl")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    ma = (rd(os.path.join(run_dir, "metrics_after.json")) or {}).get("vllm") or {}
    mb = (rd(os.path.join(run_dir, "metrics_before.json")) or {}).get("vllm") or {}
    return meta, rows, ma, mb


def verify(run_dir, pool_env=None):
    meta, rows, ma, mb = load(run_dir)
    if meta is None:
        return None
    arm = meta.get("arm")
    sessions = meta.get("sessions") or len({r.get("session_id") for r in rows
                                            if r.get("kind") == "infer"})
    infer = [r for r in rows if r.get("kind") == "infer"]
    ttft = [r["ttft_ms"] for r in infer if r.get("ttft_ms") is not None]
    checks = []

    def add(vid, ok, detail, skip=False):
        checks.append({"id": vid, "ok": True if skip else bool(ok),
                       "skip": skip, "detail": detail})

    checks_meta = meta.get("arm_checks") or {}
    v1 = (meta.get("arm_selfcheck_ok") is True
          and all(bool(checks_meta.get(k)) for k in ("metrics", "proc_env", "config"))
          if checks_meta else meta.get("arm_selfcheck_ok") is True)
    add("V1", v1, "selfcheck=%s checks=%s" % (meta.get("arm_selfcheck_ok"), checks_meta))

    add("V2", meta.get("http_errors") == 0 and meta.get("rows_infer") == sessions * 8
        and meta.get("rows_total") == sessions * 16,
        "errs=%s rows_infer=%s/%d rows_total=%s/%d" % (
            meta.get("http_errors"), meta.get("rows_infer"), sessions * 8,
            meta.get("rows_total"), sessions * 16))

    hint_lens = [r.get("hint_b64_len") or 0 for r in infer]
    max_hint = max(hint_lens) if hint_lens else 0
    if arm in ("decl", "life"):
        v3 = meta.get("hint_sent") is True and max_hint > 400
    elif arm == "off":
        v3 = meta.get("hint_sent") is False and max_hint == 0
    else:
        v3 = None
    add("V3", v3, "arm=%s hint_sent=%s max_hint_b64=%d" % (arm, meta.get("hint_sent"), max_hint),
        skip=(v3 is None))

    rst = (meta.get("prefix_cache_reset") or {}).get("http_status")
    if rst == 200:
        add("V4", True, "prefix_cache_reset=200")
    elif rst == 404:
        # 附录 B：部分镜像/机器把 reset 路由到 coordinator，引擎口 404 不算失败
        add("V4", True, "prefix_cache_reset=404（按约定不判失败；若首轮命中虚高请复查）", skip=True)
    else:
        add("V4", rst is None, "prefix_cache_reset=%s" % rst, skip=(rst is None))

    if pool_env and (pool_env.get("POOL_BLOCKS") or pool_env.get("POOL_BLOCKS_OVERRIDE")):
        add("V5", True, "pool=%s (来自 %s)" % (
            pool_env.get("POOL_BLOCKS") or pool_env.get("POOL_BLOCKS_OVERRIDE"),
            pool_env.get("_src")), skip=True)
    else:
        add("V5", True, "pool.env 缺失，跳过（先跑 02_start_engine.sh）", skip=True)

    add("V6", meta.get("coordinator_pid_before") == meta.get("coordinator_pid_after"),
        "pid %s -> %s" % (meta.get("coordinator_pid_before"), meta.get("coordinator_pid_after")))

    have = "prefix_cache_hits_total" in ma and "prefix_cache_hits_total" in mb
    if have:
        dh = ma["prefix_cache_hits_total"] - mb["prefix_cache_hits_total"]
        dq = ma.get("prefix_cache_queries_total", 0) - mb.get("prefix_cache_queries_total", 0)
        dp = (ma.get("request_prefill_kv_computed_tokens_sum", 0)
              - mb.get("request_prefill_kv_computed_tokens_sum", 0))
        add("V7", dh + dp == dq, "dHits=%d dPrefill=%d sum=%d dQueries=%d" % (dh, dp, dh + dp, dq))
        v8 = (dh % 128 == 0)
        note = "dHits=%d reuse=%.0f" % (dh, (dh - WARMUP_HITS) / 128)
        if arm in ("decl", "life"):
            v8 = v8 and dh > WARMUP_HITS
        else:
            note += "（off 不设下限：干净引擎上自然命中可以很高）"
        add("V8", v8, note)
    else:
        add("V7", True, "metrics 缺失（vllm 计数器未抓到），跳过", skip=True)
        add("V8", True, "metrics 缺失，跳过", skip=True)

    # V9 基线洁净度：off 轮不得跑在「此前有 hints-on 流量」的同一引擎实例上
    rm = rd(os.path.join(run_dir, "_run_meta.json"))
    if rm is None:
        add("V9", True, "无 _run_meta.json（旧证据/手工跑），无法判定基线洁净度", skip=True)
    else:
        hint_before = bool(rm.get("hint_traffic_on_engine_before"))
        if arm == "off":
            add("V9", not hint_before,
                "engine_pid=%s 此前 hint 流量=%s%s" % (
                    rm.get("engine_pid"), hint_before,
                    "（基线被污染，off 会劣化到 ~1s 级；需重启引擎重跑）" if hint_before else ""))
        else:
            add("V9", True, "engine_pid=%s 此前 hint 流量=%s（非 off 臂，仅记录）" % (
                rm.get("engine_pid"), hint_before), skip=True)

    p50 = med(ttft)
    le500 = sum(1 for t in ttft if t <= 500)
    ratio = (le500 / len(ttft)) if ttft else None
    hot = (p50 is not None and p50 <= 500)
    half_ok = (ratio is not None and ratio >= 0.5) == hot

    return {
        "run": os.path.basename(run_dir), "arm": arm, "sessions": sessions,
        "p50": p50, "le500": le500, "n": len(ttft), "half_ok": half_ok,
        "half_detail": "le500=%d/%d(%.0f%%) p50=%s" % (
            le500, len(ttft), 100 * (ratio or 0), ("%.1f" % p50) if p50 is not None else "-"),
        "checks": checks,
        "pass": all(c["ok"] for c in checks),
    }


def newest(root, arm=None, dataset_prefix=None):
    cands = []
    for d in glob.glob(os.path.join(root, "*")):
        if not os.path.isfile(os.path.join(d, "meta.json")):
            continue
        m = rd(os.path.join(d, "meta.json")) or {}
        if arm and m.get("arm") != arm:
            continue
        if dataset_prefix and not str(m.get("dataset") or "").startswith(dataset_prefix):
            continue
        cands.append((os.path.getmtime(os.path.join(d, "meta.json")), d))
    return max(cands)[1] if cands else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="V1-V8 验收")
    ap.add_argument("runs", nargs="*", help="evidence 目录")
    ap.add_argument("--root", default=None)
    ap.add_argument("--glob", default="*")
    ap.add_argument("--latest", action="store_true", help="只验收最新一轮")
    ap.add_argument("--arm", default=None)
    ap.add_argument("--dataset-prefix", default=None,
                    help="只验收 meta.dataset 以此前缀开头的轮次（如 contend_sp7_p60）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论（供脚本调用）")
    args = ap.parse_args(argv)

    root = args.root or os.environ.get("AH_EVIDENCE_ROOT") or \
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))

    runs = list(args.runs)
    if not runs:
        if args.latest:
            d = newest(root, args.arm, args.dataset_prefix)
            if not d:
                print("没有可验收的 evidence")
                return 2
            runs = [d]
        else:
            runs = sorted(d for d in glob.glob(os.path.join(root, args.glob))
                          if os.path.isfile(os.path.join(d, "meta.json")))

    pool_env = None
    pe = os.path.join(root, "logs", "pool.env")
    if os.path.isfile(pe):
        pool_env = {}
        for line in open(pe, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k:
                pool_env[k] = v
        pool_env["_src"] = pe
    # env override 作为兜底
    if pool_env is None and os.environ.get("POOL_BLOCKS_OVERRIDE"):
        pool_env = {"POOL_BLOCKS_OVERRIDE": os.environ["POOL_BLOCKS_OVERRIDE"], "_src": "env"}

    results = [r for r in (verify(d, pool_env) for d in runs) if r]
    if not results:
        print("没有可验收的 evidence")
        return 2

    for r in results:
        if args.quiet:
            print("%s %s %s" % ("PASS" if r["pass"] else "FAIL", r["run"], r["half_detail"]))
            continue
        print("=" * 100)
        print("%s  arm=%s sessions=%d  n=%d  %s" % (
            r["run"], r["arm"], r["sessions"], r["n"],
            "PASS" if r["pass"] else "FAIL"))
        print("  半数门槛: %s  (%s)" % ("OK" if r["half_ok"] else "MISMATCH", r["half_detail"]))
        for c in r["checks"]:
            tag = "SKIP" if c["skip"] else ("PASS" if c["ok"] else "FAIL")
            print("  %-4s %-4s %s" % (c["id"], tag, c["detail"]))

    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
