#!/usr/bin/env python3
"""Large long-pause standard scene: 16 victims + 4 attacker sessions.

Victims: one cold, one 180s pause, one resume, one finalize.
Attackers: 4 sessions; each starts after a 20s pause, then sends 16 unique
8.4k requests. Total attacker churn = 64 requests * 66 blocks.
AISBench scheduling uses delay_after_ms; model config must use_timestamp=False.
"""
import argparse
import copy
import hashlib
import json
import os
from collections import defaultdict

SRC = os.path.join(os.path.dirname(__file__), "source", "contend_lru60_S32.jsonl")
PAUSE_MS = 180000
ATTACK_DELAY_MS = 20000
VICTIMS = 16
ATTACK_SESSIONS = 4
ATTACK_TURNS = 16
OUTLEN = 64
TTL_S = 600


def parse_messages(value):
    return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)


def dump_messages(messages):
    return json.dumps(messages, ensure_ascii=False)


def load_sessions(path):
    by = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            by[row["agent_hint"]["session"]["session_id"]].append(row)
    return {
        sid: sorted(
            [r for r in rows if r.get("kind") == "infer"],
            key=lambda r: r["agent_hint"]["session"]["call_index"],
        )
        for sid, rows in by.items()
    }


def hint(sid, state, call_index, with_cc):
    h = {
        "session": {"session_id": sid, "parent_session_id": sid, "call_index": call_index, "epoch": 1},
        "agent_status": {"state": state},
        "priority": {"level": 1, "reason": "critical_path"},
    }
    if with_cc:
        h["cache_control"] = {"type": "ephemeral", "ttl_s": TTL_S, "range": {"end": 4, "unit": "message"}}
    return h


def row(question, messages, state, sid, call_index=1, delay=None, with_cc=False, max_out=OUTLEN):
    return {
        "question": question,
        "max_out_len": max_out,
        "timestamp": 0,
        "delay_after_ms": delay,
        "agent_hint": hint(sid, state, call_index, with_cc),
        "messages": dump_messages(messages),
        "tools": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    sessions = load_sessions(SRC)
    ordered = sorted(sessions)
    rows = []

    for i in range(VICTIMS):
        sid = "lpstd_victim_%02d" % i
        cold = parse_messages(sessions[ordered[i]][0]["messages"])
        resume = copy.deepcopy(cold)
        resume[-1]["content"] = (resume[-1].get("content", "") + "\n\n[resume after controlled long pause]")
        rows += [
            row("lpstd-%s-cold" % sid, cold, "running", sid, 1, with_cc=True),
            row("lpstd-%s-pause" % sid, cold, "pause", sid, 1, delay=PAUSE_MS, with_cc=True),
            row("lpstd-%s-resume" % sid, resume, "resume", sid, 2, with_cc=True),
            row("lpstd-%s-finalize" % sid, resume, "finalize", sid, 2, with_cc=True),
        ]

    for s in range(ATTACK_SESSIONS):
        sid = "lpstd_atk_%02d" % s
        short = [{"role": "user", "content": "[lpstd-attacker-arm-%02d]" % s}]
        rows.append(row("lpstd-%s-arm" % sid, short, "pause", sid, 1, delay=ATTACK_DELAY_MS, max_out=8))
        for t in range(ATTACK_TURNS):
            base = sessions[ordered[(s * ATTACK_TURNS + t) % len(ordered)]][0]
            msgs = parse_messages(base["messages"])
            marker = "[lpstd-atk-%02d-t%02d] " % (s, t)
            for m in msgs:
                if m.get("role") == "user":
                    m["content"] = marker + (m.get("content") or "")
                    break
            rows.append(row("lpstd-%s-t%02d" % (sid, t), msgs, "running", sid, t + 2))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    sids = {r["agent_hint"]["session"]["session_id"] for r in rows}
    victims = {r["agent_hint"]["session"]["session_id"] for r in rows if "victim" in r["agent_hint"]["session"]["session_id"]}
    print("wrote", args.out)
    print("rows=%d sessions=%d victims=%d attacker_sessions=%d attacker_infer=%d" % (
        len(rows), len(sids), len(victims), ATTACK_SESSIONS, ATTACK_SESSIONS * ATTACK_TURNS))
    print("victim_blocks=%d attacker_blocks=%d pause=%ss ttl=%ss" % (
        len(victims) * 66, ATTACK_SESSIONS * ATTACK_TURNS * 66, PAUSE_MS // 1000, TTL_S))
    print("md5", hashlib.md5(open(args.out, "rb").read()).hexdigest())


if __name__ == "__main__":
    main()

