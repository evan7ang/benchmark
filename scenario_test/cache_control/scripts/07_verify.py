#!/usr/bin/env python3
"""07 验收判据 V1–V11（只读 evidence；判据清单来自 config/scenario.json）。

V1  臂自检        arm_selfcheck_ok=True 且 arm_checks 三处全 True
V2  请求全通      http_errors=0；rows_infer/rows_total 与数据集实际行数一致；agent infer = sessions*8
V3  hint 一致     decl/life：agent 行 hint_b64>400；off：全部 0；
                  churn 行即使带 header 也必须 cc_raw 为空（无保护语义）
V4  前缀已重置    prefix_cache_reset.http_status=200（404 仅在该镜像无 dev 路由时容忍并标 SKIP）
V5  池子一致      pool.env.POOL_TOKENS == override*128（或与 engine-epoch.pool_blocks 一致）
V6  无中途重启    coordinator_pid_before == coordinator_pid_after
V7  省算恒等式    Δhits + Δprefill == Δqueries；churn 形态允许 0.05% 采样竞态容差
V8  复用块合理    Δhits % 128 == 0；decl/life > 16640；churn 形态下 decl 复用块 >= 0.85×理论上限
V9  基线洁净度    off 轮不得跑在「此前有 hints-on 流量」的同一引擎实例上（pid+started_ts+image_id 三元组）
V10 压力有效性    （churn 形态、off 轮）agent hit/pt <= 5% —— 证明压力真的把无保护块挤掉了
V11 收益兑现      （churn 形态、decl 轮）decl agent resume p50 <= 0.5 × 配对 off 轮的 resume p50

形态（form）决定 V9/V10/V11 的语义：clean / churn / mixed（见 scenario.json -> forms）。
"""
import argparse
import glob
import json
import os
import re
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


def scenario_cfg():
    root = os.environ.get("AH_SCENARIO_ROOT") or \
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = rd(os.path.join(root, "config", "scenario.json")) or {}
    cfg["_root"] = root
    return cfg


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


def dataset_counts(cfg, meta):
    """从生成的数据集文件数出 infer/event 行数（比按公式推更稳）。"""
    name = os.path.basename(str(meta.get("dataset") or ""))
    ddir = cfg.get("dataset", {}).get("generated_dir") or "data/generated"
    cands = [name] if name.endswith(".jsonl") else [name + ".jsonl"]
    p = next((os.path.join(cfg.get("_root", "."), ddir, n) for n in cands
              if os.path.isfile(os.path.join(cfg.get("_root", "."), ddir, n))), None)
    if not p:
        return None
    n_inf = n_ev = 0
    for line in open(p, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("kind") == "infer":
            n_inf += 1
        else:
            n_ev += 1
    return {"infer": n_inf, "event": n_ev, "total": n_inf + n_ev, "file": p}


def read_pool_env(root):
    pe = os.path.join(root, "results", "logs", "pool.env")
    if not os.path.isfile(pe):
        return None, pe
    d = {"_src": pe}
    for line in open(pe, encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if k:
            d[k] = v
    return d, pe


def verify(run_dir, cfg, form=None, pool_env=None, epoch=None, data_counts=None):
    meta, rows, ma, mb = load(run_dir)
    if meta is None:
        return None
    acfg = cfg.get("acceptance") or {}
    ap = cfg.get("analysis") or {}
    churn_prefix = ap.get("churn_session_prefix", "churn-")
    warmup = int(ap.get("warmup_hits", WARMUP_HITS))
    tol_bp = int(ap.get("identity_tolerance_bp", 5))
    forms = cfg.get("forms") or {}
    rm = rd(os.path.join(run_dir, "_run_meta.json")) or {}
    form = form or rm.get("form") or "clean"
    form_cfg = forms.get(form) or {}
    need_v10v11 = form_cfg.get("v10_v11") == "enforce"
    v9_mode = form_cfg.get("v9", "enforce")

    arm = meta.get("arm")
    infer = [r for r in rows if r.get("kind") == "infer"]
    agent = [r for r in infer if not str(r.get("session_id") or "").startswith(churn_prefix)]
    churn = [r for r in infer if str(r.get("session_id") or "").startswith(churn_prefix)]
    ag_ttft = [r["ttft_ms"] for r in agent if r.get("ttft_ms") is not None]
    res_ttft = [r["ttft_ms"] for r in agent if (r.get("call_index") or 0) >= 2 and r.get("ttft_ms") is not None]
    sessions = meta.get("sessions") or len({r.get("session_id") for r in agent})
    has_churn = len(churn) > 0
    checks = []

    def add(vid, ok, detail, skip=False):
        checks.append({"id": vid, "ok": True if skip else bool(ok), "skip": skip, "detail": detail})

    # ---- V1 ----
    checks_meta = meta.get("arm_checks") or {}
    v1 = (meta.get("arm_selfcheck_ok") is True
          and (all(bool(checks_meta.get(k)) for k in ("metrics", "proc_env", "config"))
               if checks_meta else True))
    add("V1", v1, "selfcheck=%s checks=%s" % (meta.get("arm_selfcheck_ok"), checks_meta))

    # ---- V2 ----
    exp_inf = (data_counts or {}).get("infer")
    exp_total = (data_counts or {}).get("total")
    agent_sessions = len({r.get("session_id") for r in agent})
    v2 = meta.get("http_errors") == 0 and len(agent) == agent_sessions * 8
    v2 = v2 and (exp_inf is None or meta.get("rows_infer") == exp_inf)
    v2 = v2 and (exp_total is None or meta.get("rows_total") == exp_total)
    add("V2", v2, "errs=%s rows_infer=%s/%s rows_total=%s/%s agent=%d/%d" % (
        meta.get("http_errors"), meta.get("rows_infer"), exp_inf if exp_inf is not None else "?",
        meta.get("rows_total"), exp_total if exp_total is not None else "?",
        len(agent), agent_sessions * 8))

    # ---- V3 ----
    ag_hint = [r.get("hint_b64_len") or 0 for r in agent]
    ch_hint = [r.get("hint_b64_len") or 0 for r in churn]
    ch_cc = [r.get("cc_raw") for r in churn]
    if arm in ("decl", "life"):
        v3 = meta.get("hint_sent") is True and (max(ag_hint) if ag_hint else 0) > 400
        note = "agent max_hint=%d" % (max(ag_hint) if ag_hint else 0)
        if has_churn:
            churn_cc_empty = all((not c) or c in ("{}", "", None) for c in ch_cc)
            v3 = v3 and churn_cc_empty
            note += "; churn max_hint=%d cc_empty=%s" % (
                max(ch_hint) if ch_hint else 0, churn_cc_empty)
        add("V3", v3, "arm=%s hint_sent=%s %s" % (arm, meta.get("hint_sent"), note))
    elif arm == "off":
        v3 = meta.get("hint_sent") is False and not any(ag_hint) and not any(ch_hint)
        add("V3", v3, "arm=off hint_sent=%s agent_hint_max=%d" % (
            meta.get("hint_sent"), max(ag_hint) if ag_hint else 0))
    else:
        add("V3", True, "arm=%s 未定义" % arm, skip=True)

    # ---- V4 ----
    rst = (meta.get("prefix_cache_reset") or {}).get("http_status")
    if rst == 200:
        add("V4", True, "prefix_cache_reset=200")
    elif rst == 404:
        add("V4", True, "prefix_cache_reset=404（镜像无 dev 路由；若首轮命中虚高请复查）", skip=True)
    else:
        add("V4", rst is None, "prefix_cache_reset=%s" % rst, skip=(rst is None))

    # ---- V5（真实比对：override -> banner -> pool.env -> epoch）----
    override = os.environ.get("POOL_BLOCKS_OVERRIDE") or (pool_env or {}).get("POOL_BLOCKS_OVERRIDE")
    ptok = (pool_env or {}).get("POOL_TOKENS")
    pblk = (pool_env or {}).get("POOL_BLOCKS")
    epblk = (epoch or {}).get("pool_blocks")
    if override and ptok:
        want = int(override) * 128
        ok = int(float(ptok)) == want
        add("V5", ok, "override=%s 期望 %d tokens，pool.env=%s" % (override, want, ptok))
    elif pblk and epblk:
        ok = int(float(pblk)) == int(float(epblk))
        add("V5", ok, "pool.env=%s blocks，engine-epoch=%s" % (pblk, epblk))
    else:
        add("V5", True, "缺少 pool.env / engine-epoch，无法判定（先跑 02_start_engine.sh）", skip=True)

    # ---- V6 ----
    add("V6", meta.get("coordinator_pid_before") == meta.get("coordinator_pid_after"),
        "pid %s -> %s" % (meta.get("coordinator_pid_before"), meta.get("coordinator_pid_after")))

    # ---- V7 / V8 ----
    have = "prefix_cache_hits_total" in ma and "prefix_cache_hits_total" in mb
    if have:
        dh = ma["prefix_cache_hits_total"] - mb["prefix_cache_hits_total"]
        dq = ma.get("prefix_cache_queries_total", 0) - mb.get("prefix_cache_queries_total", 0)
        dp = (ma.get("request_prefill_kv_computed_tokens_sum", 0)
              - mb.get("request_prefill_kv_computed_tokens_sum", 0))
        delta = dh + dp - dq
        if has_churn:
            tol = max(128.0, (tol_bp / 10000.0) * abs(dq))
            add("V7", abs(delta) <= tol, "dHits=%d dPrefill=%d dQueries=%d Δ=%+d (tol=%.0f)" % (
                dh, dp, dq, delta, tol))
        else:
            add("V7", delta == 0, "dHits=%d dPrefill=%d dQueries=%d Δ=%+d" % (dh, dp, dq, delta))

        reuse_all = dh / 128.0
        reuse = (max(0.0, dh - warmup) / 128.0) if has_churn else (dh - warmup) / 128.0
        v8 = (dh % 128 == 0)
        note = "dHits=%d reuse=%.0f" % (dh, reuse)
        if has_churn:
            note += "（含 warmup %.0f）" % reuse_all
        if arm in ("decl", "life"):
            v8 = v8 and dh > warmup
            if has_churn:
                theo = len(res_ttft) * 71   # 实测 protected hash 长度 66→71，用 71 作上限
                v8 = v8 and theo > 0 and reuse >= 0.85 * theo
                note += " / 理论上限=%d (%.0f%%)" % (theo, 100.0 * reuse / theo if theo else 0)
        else:
            note += "（off 不设下限）"
        add("V8", v8, note)
    else:
        add("V7", True, "metrics 缺失，跳过", skip=True)
        add("V8", True, "metrics 缺失，跳过", skip=True)

    # ---- V9（三元组判污染）----
    if rm is None:
        add("V9", True, "无 _run_meta.json，无法判定基线洁净度", skip=True)
    else:
        hint_before = bool(rm.get("hint_traffic_on_engine_before"))
        detail = "form=%s engine=(pid=%s,started_ts=%s,img=%s) 此前 hint 流量=%s" % (
            form, rm.get("engine_pid"), rm.get("engine_started_ts"),
            str(rm.get("engine_image_id") or "")[-12:], hint_before)
        if arm == "off":
            if v9_mode == "expected_fail" and hint_before:
                add("V9", True, detail + "（form=mixed：预期污染，归入生产混部形态，不作主结论）", skip=True)
            else:
                add("V9", not hint_before, detail + ("" if not hint_before else
                    "（基线被污染，off 会劣化到 ~1s 级；需重启引擎重跑）"))
        else:
            add("V9", True, detail + "（非 off 臂，仅记录）", skip=True)

    # ---- V10（压力有效性，off 轮）----
    sp_agent = sum(r.get("prompt_tokens") or 0 for r in agent)
    if need_v10v11 and has_churn and arm == "off" and have and sp_agent:
        hit_pt = 100.0 * dh / sp_agent
        add("V10", hit_pt <= 5.0, "off agent hit/pt=%.2f%%（应 <=5%%：证明压力把无保护块挤掉了）" % hit_pt)
    else:
        add("V10", True, "非 churn/off 轮，跳过", skip=True)

    # V11 由 main 配对计算（需要同数据集的 off 轮），这里先占位
    checks.append({"id": "V11", "ok": True, "skip": True, "detail": "待配对（由 main 计算）",
                   "_pending": True})

    p50 = med(ag_ttft)
    le500 = sum(1 for t in ag_ttft if t <= 500)
    ratio = (le500 / len(ag_ttft)) if ag_ttft else None
    hot = (p50 is not None and p50 <= 500)
    half_ok = (ratio is not None and ratio >= 0.5) == hot

    return {
        "run": os.path.basename(run_dir), "arm": arm, "form": form, "sessions": sessions,
        "has_churn": has_churn, "n": len(ag_ttft), "churn_n": len(churn),
        "p50": p50, "le500": le500, "resume_p50": med(res_ttft),
        "dataset": os.path.basename(str(meta.get("dataset") or "")),
        "half_ok": half_ok,
        "half_detail": "le500=%d/%d(%.0f%%) p50=%s" % (
            le500, len(ag_ttft), 100 * (ratio or 0), ("%.1f" % p50) if p50 is not None else "-"),
        "checks": checks,
        "need_v10v11": need_v10v11,
        "pass": all(c["ok"] for c in checks),
    }


def newest(root, arm=None, dataset_prefix=None, form=None):
    cands = []
    for d in glob.glob(os.path.join(root, "*")):
        if not os.path.isfile(os.path.join(d, "meta.json")):
            continue
        m = rd(os.path.join(d, "meta.json")) or {}
        if arm and m.get("arm") != arm:
            continue
        if dataset_prefix and not str(m.get("dataset") or "").startswith(dataset_prefix):
            continue
        if form:
            rm = rd(os.path.join(d, "_run_meta.json")) or {}
            if (rm.get("form") or "clean") != form:
                continue
        cands.append((os.path.getmtime(os.path.join(d, "meta.json")), d))
    return max(cands)[1] if cands else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="V1-V11 验收")
    ap.add_argument("runs", nargs="*", help="evidence 目录")
    ap.add_argument("--root", default=None)
    ap.add_argument("--glob", default="*")
    ap.add_argument("--latest", action="store_true", help="只验收最新一轮")
    ap.add_argument("--arm", default=None)
    ap.add_argument("--form", default=None)
    ap.add_argument("--dataset-prefix", default=None,
                    help="只验收 meta.dataset 以此前缀开头的轮次（如 contend_sp7churn_p60_S8_L2）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论（供脚本调用）")
    args = ap.parse_args(argv)

    cfg = scenario_cfg()
    root = args.root or os.environ.get("AH_EVIDENCE_ROOT") or os.path.join(cfg["_root"], "results")

    runs = list(args.runs)
    if not runs:
        if args.latest:
            d = newest(root, args.arm, args.dataset_prefix, args.form)
            if not d:
                print("没有可验收的 evidence")
                return 2
            runs = [d]
        else:
            runs = sorted(d for d in glob.glob(os.path.join(root, args.glob))
                          if os.path.isfile(os.path.join(d, "meta.json")))

    pool_env, _ = read_pool_env(cfg["_root"])
    epoch = rd(os.path.join(root, "logs", "engine-epoch.json"))

    results = []
    for d in runs:
        meta = rd(os.path.join(d, "meta.json")) or {}
        dc = dataset_counts(cfg, meta)
        r = verify(d, cfg, args.form, pool_env, epoch, dc)
        if r:
            results.append(r)
    if not results:
        print("没有可验收的 evidence")
        return 2

    # ---- V11：为 churn 形态的 decl 轮配对同数据集 off 轮，比较 resume p50 ----
    churn_prefix = (cfg.get("analysis") or {}).get("churn_session_prefix", "churn-")

    def off_resume_p50(root, dataset, form, want_churn):
        """在证据根目录里找同数据集、同形态的 off 轮，返回最小 resume p50。"""
        vals = []
        for dd in glob.glob(os.path.join(root, "*")):
            mm = rd(os.path.join(dd, "meta.json"))
            if not mm or mm.get("arm") != "off":
                continue
            if os.path.basename(str(mm.get("dataset") or "")) != dataset:
                continue
            rmm = rd(os.path.join(dd, "_run_meta.json")) or {}
            if form and (rmm.get("form") or "clean") != form:
                continue
            rr = [json.loads(x) for x in open(os.path.join(dd, "requests.jsonl"), encoding="utf-8") if x.strip()] \
                if os.path.isfile(os.path.join(dd, "requests.jsonl")) else []
            has_ch = any(str(r0.get("session_id") or "").startswith(churn_prefix) for r0 in rr)
            if has_ch != want_churn:
                continue
            res = [r0["ttft_ms"] for r0 in rr
                   if (r0.get("call_index") or 0) >= 2 and r0.get("ttft_ms") is not None
                   and not str(r0.get("session_id") or "").startswith(churn_prefix)]
            if res:
                vals.append(med(res))
        return min(vals) if vals else None

    for r in results:
        if not (r["need_v10v11"] and r["has_churn"] and r["arm"] == "decl"):
            continue
        v11 = next((c for c in r["checks"] if c["id"] == "V11"), None)
        if v11 is None:
            continue
        cands = [o for o in results
                 if o["has_churn"] and o["arm"] == "off" and o["dataset"] == r["dataset"]]
        off_res = min((c["resume_p50"] for c in cands if c["resume_p50"] is not None), default=None)
        if off_res is None:
            off_res = off_resume_p50(root, r["dataset"], r["form"], True)
        if off_res is None or r["resume_p50"] is None:
            v11["detail"] = "缺少配对 off 轮（同数据集），V11 无法判定（先跑 off 轮）"
            continue
        ok = r["resume_p50"] <= 0.5 * off_res
        v11.update({"ok": bool(ok), "skip": False,
                    "detail": "decl resume p50=%.1f vs off 最佳 resume p50=%.1f（要求 <=%.1f）" % (
                        r["resume_p50"], off_res, 0.5 * off_res)})
        r["pass"] = all(c["ok"] for c in r["checks"])

    for r in results:
        if args.quiet:
            print("%s %s form=%s arm=%s %s" % (
                "PASS" if r["pass"] else "FAIL", r["run"], r["form"], r["arm"], r["half_detail"]))
            continue
        print("=" * 100)
        print("%s  form=%s arm=%s sessions=%d agent_n=%d churn_n=%d  %s" % (
            r["run"], r["form"], r["arm"], r["sessions"], r["n"], r["churn_n"],
            "PASS" if r["pass"] else "FAIL"))
        print("  半数门槛: %s  (%s)" % ("OK" if r["half_ok"] else "MISMATCH", r["half_detail"]))
        for c in r["checks"]:
            tag = "SKIP" if c["skip"] else ("PASS" if c["ok"] else "FAIL")
            print("  %-4s %-4s %s" % (c["id"], tag, c["detail"]))

    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
