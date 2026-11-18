#!/usr/bin/env python3
"""生成短 pause 场景数据集（sp7：每个会话 7 次 resume）。

行布局（每会话）：
    infer(running) [pause event] infer(resume) ×7 ... finalize event

参数语义：
  * 每次 infer 之后跟一个 pause 事件，pause 窗 60 s => 7/8 次调用是 resume（87.5%）
  * 调用槽 D=8 s（max_out_len=64，decode 约 0.8 s，8 s 足够）
  * cache_control.ttl_s=120，必须 > pause（60 s），否则保护中途到期

本脚本是**纯离线变换**：只读冻结源 contend_lru60_S32.jsonl，不需要引擎、不联网。
输出对同一输入是确定性字节级可复现的（sha256 见 data/README.md 与 config/scenario.json）。

外部 churn（形态 C）：
    --churn-intervals-ms 2000,1000,700  为每个 N 额外生成 l3 档压力数据集
      contend_sp7churn_p60_S<N>_L<k>.jsonl = agent 行（与无 churn 版逐字节相同）+ churn 行
    churn 行是「永不命中的一次性冷流量」：长文档按偏移切片（步长 400 字符 > 一个
    128-token 块长）、无 cache_control、max_out_len=8，按固定间隔铺满 design span。
    它只制造**块淘汰压力**，不携带保护语义（decl 臂即使发 header 也无 cc）。

用法：
    python3 gen_sp7.py --sessions 1,4,8,32
    python3 gen_sp7.py --sessions 8 --outdir /tmp/x
    python3 gen_sp7.py --sessions 8 --churn-intervals-ms 2000,1000,700
"""
import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(HERE, "source", "contend_lru60_S32.jsonl")
DEFAULT_OUT = os.path.join(HERE, "generated")


def build_session_rows(by, sid, t0, P, D, OUTLEN, TTL):
    src = sorted(by[sid], key=lambda r: r["agent_hint"]["session"]["call_index"])
    infer = [r for r in src if r.get("kind") == "infer"]
    ev = [r for r in src if r.get("kind") != "infer"]
    # 源会话是 K 次调用 + 1 次 finalize；prompts = infer 行的 messages + finalize 行 messages
    # 这里需要 k=1..8 共 8 段，即 infer 行至少 7 条（第 8 段取 finalize 行）
    if len(infer) < 7:
        raise SystemExit("source session %s has %d infer rows, need >= 7" % (sid, len(infer)))
    prompts = [copy.deepcopy(r["messages"]) for r in infer] + [copy.deepcopy(ev[0]["messages"])]

    rows = []
    for k in range(1, 9):
        t = t0 + (k - 1) * (P + D)
        r = copy.deepcopy(infer[0])
        r["timestamp"] = t
        r["t_start_ms"] = t
        r["kind"] = "infer"
        r["max_out_len"] = OUTLEN
        r["messages"] = prompts[k - 1]
        r["delay_after_ms"] = 1500
        r["resume_before"] = True if k >= 2 else None
        h = r["agent_hint"]
        h["session"]["call_index"] = k
        h["session"]["epoch"] = 1
        h["agent_status"] = {"state": "running" if k == 1 else "resume", "state_since_ms": t}
        h["cache_control"] = {"type": "ephemeral", "ttl_s": TTL,
                              "range": {"end": 4, "unit": "message"}}
        h["prediction"] = {"latency_ms": 1000, "output_tokens": OUTLEN, "remaining_calls": 8 - k}
        rows.append(r)

        if k < 8:
            e = copy.deepcopy(infer[0])
            e["timestamp"] = t + D
            e["t_start_ms"] = t + D
            e["kind"] = "event"
            e["max_out_len"] = OUTLEN
            e["messages"] = prompts[k - 1]
            e["delay_after_ms"] = None
            e["resume_before"] = None
            eh = e["agent_hint"]
            eh["session"]["call_index"] = k
            eh["agent_status"] = {"state": "pause", "state_since_ms": t + D}
            eh["cache_control"] = {"type": "ephemeral", "ttl_s": TTL,
                                   "range": {"end": 4, "unit": "message"}}
            eh["prediction"] = {"latency_ms": 1000, "output_tokens": OUTLEN, "remaining_calls": 8 - k}
            rows.append(e)

    tf = t0 + 7 * (P + D) + D
    f = copy.deepcopy(infer[0])
    f["timestamp"] = tf
    f["t_start_ms"] = tf
    f["kind"] = "event"
    f["max_out_len"] = 256
    f["messages"] = prompts[7]
    f["delay_after_ms"] = None
    f["resume_before"] = None
    fh = f["agent_hint"]
    fh["session"]["call_index"] = 8
    fh["agent_status"] = {"state": "finalize", "state_since_ms": tf}
    fh["cache_control"] = {"type": "ephemeral", "ttl_s": 60,
                           "range": {"end": 4, "unit": "message"}}
    fh["prediction"] = {"latency_ms": 1000, "output_tokens": OUTLEN, "remaining_calls": 0}
    rows.append(f)
    return rows


def build(by, sids, P, D, STAG, OUTLEN, TTL, BASE):
    rows = []
    for i, sid in enumerate(sids):
        rows.extend(build_session_rows(by, sid, BASE + i * STAG, P, D, OUTLEN, TTL))
    rows.sort(key=lambda r: (r["timestamp"], r["agent_hint"]["session"]["session_id"]))
    return rows


# ---------------------------------------------------------------------------
# 外部 churn（形态 C）：一次性冷流量，制造块淘汰压力但不带保护语义
# ---------------------------------------------------------------------------
CHURN_CHARS = 2700      # ≈2,900 token ≈22.7 块/请求（实测反向标定，见 docs/DESIGN.md §9）
CHURN_STEP = 400        # 偏移步长 > 一个 128-token 块长，保证相邻切片首块不同
CHURN_SPAN_MS = 600000  # 覆盖 S8 的 design span(540 s) + pause 收尾余量


def churn_prefix_pool(by, chars=CHURN_CHARS, step=CHURN_STEP):
    """从所有源会话的长文档按偏移切片，按文档去重，生成唯一前缀池。

    不与 agent 前缀冲突：agent 用的是完整长文档（8,609 字符），churn 用的是
    2,700 字符切片，内容不同 ⇒ 首块 hash 不同 ⇒ 永不命中 agent 前缀。
    前缀池深度只需覆盖「块在 free queue 的存活窗口」(实测 <20 s)，
    超出后复用仍全 miss（旧块早被 pop）。
    """
    docs, seen = [], set()
    for sid in sorted(by):
        for x in by[sid]:
            for m in x["messages"]:
                c = m.get("content")
                if isinstance(c, str) and len(c) > 4000 and c not in seen:
                    seen.add(c)
                    docs.append(c)
    pool = []
    for c in docs:
        off = 0
        while off + chars <= len(c):
            pool.append(c[off:off + chars])
            off += step
    return pool


def build_churn(by, pool, interval_ms, span_ms, outlen, base_ts):
    tmpl = by[sorted(by)[-1]][0]
    n = span_ms // interval_ms + 1
    rows = []
    for i in range(n):
        r = copy.deepcopy(tmpl)
        r["timestamp"] = base_ts + i * interval_ms
        r["t_start_ms"] = r["timestamp"]
        r["kind"] = "infer"
        r["question"] = "churn-%04d" % i
        r["max_out_len"] = outlen
        msgs = copy.deepcopy(tmpl["messages"])
        for m in msgs:
            c = m.get("content")
            if isinstance(c, str) and len(c) > 4000:
                m["content"] = pool[i % len(pool)]
                break
        r["messages"] = msgs
        r["delay_after_ms"] = None
        r["resume_before"] = None
        h = r["agent_hint"]
        h["session"] = {"session_id": "churn-%04d" % i, "agent_type": "churn",
                        "call_index": 1, "epoch": 1, "parent_session_id": None}
        h["agent_status"] = {"state": "running", "state_since_ms": r["timestamp"]}
        h.pop("cache_control", None)          # 关键：无保护声明
        h["priority"] = {"level": 0, "reason": "background"}
        h["prediction"] = {"latency_ms": 500, "output_tokens": outlen, "remaining_calls": 0}
        rows.append(r)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 sp7 短 pause 数据集（离线、确定性）")
    ap.add_argument("--source", default=DEFAULT_SRC, help="冻结源 jsonl（默认 data/source/contend_lru60_S32.jsonl）")
    ap.add_argument("--outdir", default=DEFAULT_OUT, help="输出目录（默认 data/generated）")
    ap.add_argument("--sessions", default="1,4,8,32",
                    help="逗号分隔的会话数列表；每个 N 输出 contend_sp7_p60_S<N>.jsonl")
    ap.add_argument("--pause-ms", type=int, default=60000)
    ap.add_argument("--slot-ms", type=int, default=8000)
    ap.add_argument("--stagger-ms", type=int, default=8000)
    ap.add_argument("--max-out-len", type=int, default=64)
    ap.add_argument("--ttl-s", type=int, default=120)
    ap.add_argument("--base-ts", type=int, default=1794600000000)
    ap.add_argument("--manifest", action="store_true", default=True,
                    help="同时生成 manifest_contend.json（默认开）")
    ap.add_argument("--no-manifest", dest="manifest", action="store_false")
    ap.add_argument("--churn-intervals-ms", default="",
                    help="逗号分隔的 churn 间隔（如 2000,1000,700）；非空时额外生成 *_L<k> 数据集")
    ap.add_argument("--churn-span-ms", type=int, default=CHURN_SPAN_MS)
    ap.add_argument("--churn-chars", type=int, default=CHURN_CHARS)
    ap.add_argument("--churn-out-len", type=int, default=8)
    args = ap.parse_args(argv)

    by = {}
    for line in open(args.source, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        by.setdefault(r["agent_hint"]["session"]["session_id"], []).append(r)
    order = sorted(by)
    total = len(order)

    os.makedirs(args.outdir, exist_ok=True)
    import hashlib

    def sha256_of(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    datasets = []
    churn_intervals = [int(x) for x in args.churn_intervals_ms.split(",") if x.strip()]
    for n in [int(x) for x in args.sessions.split(",") if x.strip()]:
        if n > total:
            raise SystemExit("requested sessions=%d but source only has %d" % (n, total))
        sids = order[:n]
        agent_rows = build(by, sids, args.pause_ms, args.slot_ms, args.stagger_ms,
                           args.max_out_len, args.ttl_s, args.base_ts)
        name = "contend_sp7_p60_S%d.jsonl" % n
        path = os.path.join(args.outdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in agent_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        n_infer = sum(1 for r in agent_rows if r.get("kind") == "infer")
        print("wrote %-38s rows=%3d infer=%3d sha256=%s" % (
            name, len(agent_rows), n_infer, sha256_of(path)))
        datasets.append({"name": os.path.splitext(name)[0], "file": name,
                         "sessions": n, "rows": len(agent_rows), "infer": n_infer,
                         "sha256": sha256_of(path), "_peak": n})

        if not churn_intervals:
            continue
        pool = churn_prefix_pool(by, args.churn_chars)
        for k, iv in enumerate(churn_intervals, start=1):
            churn_rows = build_churn(by, pool, iv, args.churn_span_ms,
                                     args.churn_out_len, args.base_ts)
            rows = agent_rows + churn_rows
            rows.sort(key=lambda r: (r["timestamp"],
                                     r["agent_hint"]["session"]["session_id"]))
            cname = "contend_sp7churn_p60_S%d_L%d.jsonl" % (n, k)
            cpath = os.path.join(args.outdir, cname)
            with open(cpath, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n_churn = len(churn_rows)
            print("wrote %-38s rows=%3d infer=%3d agent=%d churn=%d sha256=%s" % (
                cname, len(rows), n_infer + n_churn, n_infer, n_churn, sha256_of(cpath)))
            datasets.append({
                "name": os.path.splitext(cname)[0], "file": cname,
                "sessions": n, "churn_sessions": n_churn,
                "churn_interval_ms": iv, "churn_chars": args.churn_chars,
                "churn_span_ms": args.churn_span_ms,
                "rows": len(rows), "infer": n_infer + n_churn,
                "agent_infer": n_infer, "churn_infer": n_churn,
                "sha256": sha256_of(cpath), "_peak": n,
            })

    if args.manifest:
        mpath = os.path.join(args.outdir, "manifest_contend.json")
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump({"datasets": datasets}, fh, ensure_ascii=False, indent=2)
        print("wrote manifest_contend.json (%d datasets)" % len(datasets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
