#!/usr/bin/env python3
"""生成「过订比扫描」数据集（bp / gp / gc 系）：短 pause + 外部 churn 压力。

与 `gen_sp7.py` 的区别
----------------------
* `gen_sp7.py` 从冻结真实 capture 切行，pause 60 s、会话内 8 次调用，回答「hint 本身是加速还是开销」；
* 本脚本**纯合成**（无外部依赖、可离线逐字节复现），把两个真正的自变量做成旋钮：
    - `live_chars` / `live_n`  → live 工作集大小 → **过订比** `live_tok / pool_tok`
    - `churn_chars` / `churn_n`→ 外部淘汰压力强度
  它回答的是「**hint 什么时候能换成 TTFT**」，即收益边界，而不是有无收益。

行布局
------
    live 会话 : 12 次 infer（间隔 HOT=28 s）+ 每次 infer 后 8 s 一个 event 行
    churn 会话: 60 次 infer，前缀按 (会话, 轮次) 唯一 ⇒ 永不命中，纯淘汰压力
    churn 行**不带 cache_control**（decl 臂上也一样），只制造块淘汰

参数语义（改之前先读）
----------------------
* `HOT=28000 ms` 是同一 live 会话相邻两次 infer 的间隔，即 **pause ≈ 28 s**；live 跨度为
  `(live_rounds-1)×HOT ≈ 308 s`。ttl 的阈值判据是**会话跨度**，不是 pause（见
  `docs/RESULTS_2026-11-18_profit_map.md` §5）。
* `--ttl 60`（默认）复现 2026-11-18 落盘的实测数据集；`--ttl 0` 生成**只带 range 不带 ttl_s**
  的版本 —— 实测这一版与「完全不发 cache_control」在命中上逐位相同，且不会因 ttl 小于
  会话跨度而提前降级，**生产默认建议用 `--ttl 0`**。
* `cache_control` 只出现在 `agent_hint.cache_control`（header 版）；`_nocc` 孪生用
  `--strip-cc` 生成，除该键外逐行与原件相同。

用法
----
    python3 gen_profit.py                          # 全部 SPECS，输出 data/generated/
    python3 gen_profit.py bp25 bp70                # 指定数据集
    python3 gen_profit.py bp70 --ttl 0 --outdir /tmp/x
    python3 gen_profit.py --strip-cc bp25 bp45     # 生成 bp25_nocc / bp45_nocc 孪生
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "generated")

SYS = "You are a benchmark replay agent. Answer with a short status line only."
BASE = 1796000000000
HOT = 28000      # live 相邻两次 infer 的间隔（ms）：resume 点 = 上一次 infer + 28 s
EV = 8000        # live event 行相对 infer 的偏移
GRID = 8000      # churn 轮间隔
DLY = 1500
MO = 64


def filler(prefix, n):
    """生成 n 个字符的稳定文本块；同 (prefix, n) ⇒ 逐字节相同，便于前缀复用。"""
    out, i = [], 0
    while sum(len(x) for x in out) < n:
        out.append("%s%06d " % (prefix, i))
        i += 1
    return "".join(out)[:n]


def hint(sid, cidx, remain, cc, ttl):
    h = {"session": {"session_id": sid, "agent_type": "agent", "call_index": cidx,
                     "epoch": 1, "parent_session_id": sid},
         "agent_status": {"state": "running", "state_since_ms": BASE},
         "priority": {"level": 1, "reason": "critical_path"},
         "prediction": {"latency_ms": 1000, "output_tokens": MO, "remaining_calls": remain}}
    if cc:
        # 键序与 2026-11-18 落盘的数据集一致（type, ttl_s, range），保证逐字节可复现
        c = {"type": "ephemeral"}
        if ttl:
            c["ttl_s"] = int(ttl)
        c["range"] = {"end": 4, "unit": "message"}
        h["cache_control"] = c
    return h


def row(kind, q, msgs, h, t):
    return {"kind": kind, "question": q, "messages": msgs, "agent_hint": h,
            "delay_after_ms": DLY, "enable_thinking": False, "max_out_len": MO,
            "resume_before": None, "t_start_ms": t, "timestamp": t, "tools": None}


def gen(tag, live_n, live_rounds, live_chars, churn_n, churn_rounds, churn_chars,
        cc=True, ttl=60):
    rows = []
    for li in range(live_n):
        sid = "live%02d" % li
        live_pref = filler("8d2d6et%02d" % li, live_chars)   # 每个会话独立前缀
        for r in range(live_rounds):
            t = BASE + r * HOT + li * 120
            msgs = [{"role": "system", "content": SYS},
                    {"role": "user", "content": live_pref},
                    {"role": "assistant", "content": "OK\n"},
                    {"role": "user", "content": filler("%s-r%03d" % (sid, r), 456)}]
            rows.append(row("infer", "%s-%s" % (tag, sid), msgs,
                            hint(sid, r + 1, live_rounds - r, cc, ttl), t))
            rows.append(row("event", "%s-%s" % (tag, sid), msgs,
                            hint(sid, r + 1, live_rounds - r, cc, ttl), t + EV))
    for ci in range(churn_n):
        sid = "churn%02d" % ci
        for r in range(churn_rounds):
            t = BASE + r * GRID + 200 + ci * 400
            # 每个 (会话, 轮次) 前缀唯一 ⇒ 真正制造 churn
            pref = filler("c%s%03d" % (sid[-2:], r), churn_chars - 456)
            msgs = [{"role": "system", "content": SYS},
                    {"role": "user", "content": pref},
                    {"role": "assistant", "content": "OK\n"},
                    {"role": "user", "content": filler("%s-r%03d" % (sid, r), 456)}]
            rows.append(row("infer", "%s-%s" % (tag, sid), msgs,
                            hint(sid, r + 1, churn_rounds - r, False, 0), t))
    rows.sort(key=lambda x: x["t_start_ms"])
    return rows


# 名字里的数字 = 设计过订比（live 工作集占池比例），不是实测值；实测见 results 文档。
SPECS = {
    "bigp_live8": dict(tag="bp8", live_n=8, live_rounds=12, live_chars=20000,
                       churn_n=12, churn_rounds=60, churn_chars=2500),
    "bigp_live12": dict(tag="bp12", live_n=12, live_rounds=12, live_chars=20000,
                        churn_n=12, churn_rounds=60, churn_chars=2500),
    "bigp_live20": dict(tag="bp20", live_n=20, live_rounds=12, live_chars=20000,
                        churn_n=12, churn_rounds=60, churn_chars=2500),
    "bp25": dict(tag="b25", live_n=8, live_rounds=12, live_chars=8000,
                 churn_n=12, churn_rounds=60, churn_chars=1356),
    "bp45": dict(tag="b45", live_n=14, live_rounds=12, live_chars=8000,
                 churn_n=12, churn_rounds=60, churn_chars=1356),
    "bp70": dict(tag="b70", live_n=20, live_rounds=12, live_chars=9000,
                 churn_n=12, churn_rounds=60, churn_chars=1356),
    "bpdom": dict(tag="bdm", live_n=24, live_rounds=20, live_chars=8000,
                  churn_n=6, churn_rounds=30, churn_chars=4000),
    "gp25": dict(tag="g25", live_n=8, live_rounds=12, live_chars=8000,
                 churn_n=4, churn_rounds=22, churn_chars=7000),
    "gp45": dict(tag="g45", live_n=14, live_rounds=12, live_chars=8000,
                 churn_n=4, churn_rounds=22, churn_chars=7000),
    "gp69": dict(tag="g69", live_n=20, live_rounds=12, live_chars=9000,
                 churn_n=4, churn_rounds=22, churn_chars=7000),
    "gc50": dict(tag="c50", live_n=14, live_rounds=12, live_chars=8000,
                 churn_n=5, churn_rounds=30, churn_chars=10500),
    "gc70": dict(tag="c70", live_n=20, live_rounds=12, live_chars=9000,
                 churn_n=5, churn_rounds=30, churn_chars=10500),
    "bpdl": dict(tag="dl", live_n=24, live_rounds=20, live_chars=8000,
                 churn_n=6, churn_rounds=10, churn_chars=4000),
}


def write(rows, path):
    with open(path, "w", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def strip_cc(src_path, dst_path):
    """生成 _nocc 孪生：只摘 agent_hint.cache_control，其余逐行相同。"""
    src = [json.loads(l) for l in open(src_path) if l.strip()]
    out, n_cc = [], 0
    for r in src:
        h = r.get("agent_hint")
        if isinstance(h, dict) and "cache_control" in h:
            h = dict(h)
            h.pop("cache_control")
            r = dict(r)
            r["agent_hint"] = h
            n_cc += 1
        out.append(r)
    with open(dst_path, "w", newline="\n") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n_cc


def stats(name, rows):
    inf = [r for r in rows if r["kind"] == "infer"]
    live = [r for r in inf if r["question"].split("-")[-1].startswith("live")]
    churn = [r for r in inf if r["question"].split("-")[-1].startswith("churn")]
    lv = sum(len(r["messages"][1]["content"]) for r in live) // max(1, len({r["question"] for r in live}))
    ch = sum(len(r["messages"][1]["content"]) for r in churn)
    span = (rows[-1]["t_start_ms"] - rows[0]["t_start_ms"]) // 1000
    print("%-12s rows=%-5d infer=%-5d live_infer=%-4d churn=%-4d span=%ds "
          "live_pref=%dch churn_tot=%dkch"
          % (name, len(rows), len(inf), len(live), len(churn), span, lv, ch // 1000))


def main():
    ap = argparse.ArgumentParser(description="过订比扫描数据集生成器（短 pause + churn）")
    ap.add_argument("names", nargs="*", help="要生成的数据集名（默认全部 SPECS）")
    ap.add_argument("--outdir", default=DEFAULT_OUT)
    ap.add_argument("--ttl", type=int, default=60,
                    help="cache_control.ttl_s；0 表示不写 ttl_s（推荐，见模块 docstring）")
    ap.add_argument("--strip-cc", action="store_true",
                    help="额外生成 <name>_nocc.jsonl（只摘 cache_control 的孪生）")
    args = ap.parse_args()

    want = args.names or list(SPECS)
    unknown = [n for n in want if n not in SPECS]
    if unknown:
        raise SystemExit("未知数据集：%s（可选：%s）" % (", ".join(unknown), ", ".join(SPECS)))
    os.makedirs(args.outdir, exist_ok=True)
    for name in want:
        rows = gen(**SPECS[name], ttl=args.ttl)
        p = os.path.join(args.outdir, name + ".jsonl")
        write(rows, p)
        stats(name, rows)
        if args.strip_cc:
            n = strip_cc(p, os.path.join(args.outdir, name + "_nocc.jsonl"))
            print("%-12s -> %s_nocc.jsonl（摘掉 cc 的行数=%d）" % ("", name, n))


if __name__ == "__main__":
    main()
