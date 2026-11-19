#!/usr/bin/env python3
"""gen_real.py -- realism-first agent-session trace (V21 family).

Why this shape (each point answers a defect measured on the earlier traces):

* no pause rows at all.  A pause in a real agent trace is idle time, not an
  HTTP request; the pause rows used earlier were 1.6 k-token requests with a
  ~0.4 s TTFT that only inflated the denominator of mean TTFT by ~29 % and
  diluted every saving.  Idle time is expressed with delay_after_ms, and the
  cache_control TTL (>= the largest gap) keeps the session alive across it.
* context accumulates every turn to TURNS_MAX (~39.4 k, the hard cap of
  /home/RAG_data/models/Qwen3-8B: max_position_embeddings=40960, rope_scaling
  =None, so a longer single request is not legal without YaRN).
* many sessions per instance, and the sum of their live contexts deliberately
  exceeds one card's KV pool (259 968 tok = 2 031 blocks).  Oversubscription is
  what a real shared endpoint looks like, and it is the *precondition* for the
  protection to save anything: if every session's prefix fits, nothing is ever
  evicted and arm differences are exactly zero (measured on V19).
* a background tenant (churn) of unique one-shot 20 k requests, low rate, that
  keeps a couple of requests in flight.  It is the "other traffic" that turns
  the pool over; it is not the main pressure, the sessions are.
* only the hottest subset of sessions carries cache_control
  (N_PROTECT of N_SESS).  A deployment cannot protect more than the pool can
  hold: pinned blocks are neither allocatable nor reclaimable
  (vllm/v1/core/block_pool.py:798-808), so K must stay under ~0.9 * pool or the
  engine deadlocks.

Per-instance arithmetic printed at the end; the guard is K + in-flight <= 0.95*P.

env: OUT_DIR VARIANT SHARD SHARDS N_SESS N_PROTECT TURNS TURNS_MAX GAP_LO GAP_HI
     N_CHURN CHURN_TOK CHURN_GAP PROT_TTL SHARED SEED
"""
import hashlib
import json
import os
import random

OUT_DIR = os.environ.get("OUT_DIR", ".")
VARIANT = os.environ.get("VARIANT", "V21")
SHARD = int(os.environ.get("SHARD", "0"))
SHARDS = int(os.environ.get("SHARDS", "1"))
N_SESS = int(os.environ.get("N_SESS", "8"))
N_PROTECT = int(os.environ.get("N_PROTECT", "5"))
TURNS = int(os.environ.get("TURNS", "10"))
TURNS_BASE = int(os.environ.get("TURNS_BASE", "5000"))
TURNS_MAX = int(os.environ.get("TURNS_MAX", "39400"))
JITTER_MAX = float(os.environ.get("JITTER_MAX", "6"))
GAP_LO = float(os.environ.get("GAP_LO", "15"))
GAP_HI = float(os.environ.get("GAP_HI", "240"))
N_CHURN = int(os.environ.get("N_CHURN", "30"))
CHURN_TOK = int(os.environ.get("CHURN_TOK", "20000"))
CHURN_GAP = float(os.environ.get("CHURN_GAP", "1.2"))
PROT_TTL = int(os.environ.get("PROT_TTL", "300"))
SHARED = int(os.environ.get("SHARED", "2000"))
SEED = int(os.environ.get("SEED", "1121"))

BPT = 1.120                 # measured ~1.12 tokens per word on this tokenizer
# Measured on V20: a 20 019-char body of "t%08x" words tokenized to 15 784
# prompt tokens, i.e. 7.9 tokens per 10-char word.  The earlier traces were
# therefore ~21 % shorter than their nominal size (a "38 k" session really was
# ~30 k).  Validate against prompt_tokens in recs.json, never against intent.
TOK_PER_WORD = float(os.environ.get("TOK_PER_WORD", "7.9"))
SYS_TOK = 15
SYS_TEXT = "You are a coding agent replaying a real multi-turn session. Answer with one short status line."
ASST_TOK = 32
ASST_TEXT = "ok " * 32
BLK = 128

rng = random.Random(SEED + 1000 * SHARD)


def _stream(tag, n):
    x = int.from_bytes(hashlib.sha256(tag.encode()).digest()[:8], "big") | 1
    out = []
    for _ in range(n):
        x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 7
        x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
        out.append("t%08x" % (x & 0xFFFFFFFF))
    return " ".join(out)


def fill(n_tok, tag):
    n = max(1, int(round(n_tok / float(TOK_PER_WORD))))
    return _stream(tag, n) + " "


def sys_text():
    return SYS_TEXT + " " + fill(max(1, SHARED - SYS_TOK), "shared-sys")


def prefill_s(tok):
    """Conservative prefill estimate; 40 k prompts do not run at short-context
    speed, so the long end is deliberately pessimistic."""
    return max(0.35, tok / (8000.0 if tok < 8000 else 3500.0))


def ramp():
    """Per-turn context sizes: cold 5 k, then +step each turn up to TURNS_MAX."""
    step = (TURNS_MAX - TURNS_BASE) / float(max(1, TURNS - 2))
    t = [TURNS_BASE]
    for _ in range(TURNS - 1):
        t.append(min(TURNS_MAX, int(t[-1] + step)))
    return t


def messages_for(s, t):
    stext = sys_text()
    lens = [max(1, s["tgt"][0] - SHARED)]
    for i in range(1, t + 1):
        lens.append(max(1, s["tgt"][i] - s["tgt"][i - 1] - ASST_TOK))
    ms = [{"role": "system", "content": stext}]
    for i in range(t + 1):
        ms.append({"role": "user", "content": fill(lens[i], "%s-u%d" % (s["sid"], i))})
        if i < t:
            ms.append({"role": "assistant", "content": ASST_TEXT})
    return ms


def churn_messages(i):
    body = fill(CHURN_TOK - SHARED - 40, "churn-%s-s%d-%d" % (VARIANT, SHARD, i)) + " churn%d" % i
    return [{"role": "system", "content": sys_text()},
            {"role": "user", "content": body}]


def build():
    sess = []
    for v in range(N_SESS):
        sid = "real-%s-s%d-v%02d" % (VARIANT.lower(), SHARD, v)
        sess.append(dict(sid=sid, tgt=ramp(), prot=(v < N_PROTECT),
                         jitter=rng.uniform(0, JITTER_MAX)))
    events = []
    for s in sess:
        T = s["jitter"]
        for t in range(TURNS):
            events.append([T, "v", s, t])
            T += prefill_s(s["tgt"][t]) + rng.uniform(GAP_LO, GAP_HI)
    span = max(e[0] for e in events)
    step = max(CHURN_GAP, span / float(max(1, N_CHURN)))
    for c in range(N_CHURN):
        events.append([rng.uniform(0.4, span), "c", c, 0])
    events.sort(key=lambda e: (e[0], 0 if e[1] == "c" else 1))

    rows = []
    for k, (T, kind, ref, t) in enumerate(events):
        nxt = events[k + 1][0] if k + 1 < len(events) else T
        if kind == "v":
            cost = prefill_s(ref["tgt"][t])
        else:
            cost = prefill_s(CHURN_TOK)
        delay = max(0.2, (nxt - T) - cost)
        rows.append((delay, kind, ref, t))
    return sess, rows, step


def make_row(kind, ref, t, delay, with_cc):
    if kind == "v":
        sid = ref["sid"]
        ms = messages_for(ref, t)
        q = "%s-t%d" % (sid, t)
        out_len = 96
        st, pr = "running", {"level": 1, "reason": "critical_path"}
        ttl, n_turns = PROT_TTL, TURNS
        prot = ref["prot"]
    else:
        sid = "real-%s-s%d-c%02d" % (VARIANT.lower(), SHARD, ref)
        ms = churn_messages(ref)
        q = "%s-one" % sid
        out_len, st, pr, ttl, n_turns, prot = 32, "running", {"level": 3, "reason": "batch"}, 1, 1, False
    hint = {
        "session_id": sid,
        "session": {"session_id": sid, "agent_type": "agent",
                    "call_index": t + 1, "epoch": 1, "parent_session_id": sid},
        "agent_status": {"state": st, "state_since_ms": 1796000000000 + int(delay * 1000)},
        "priority": pr,
        "prediction": {"latency_ms": 2000, "output_tokens": out_len,
                       "remaining_calls": max(0, n_turns - t - 1)},
    }
    if with_cc and prot:
        hint["cache_control"] = {"type": "ephemeral", "ttl": ttl,
                                 "block_offset": 100000, "ttl_s": ttl,
                                 "range": {"end": 99999, "unit": "message"}}
    return {
        "question": q,
        "max_out_len": out_len,
        "timestamp": 0,
        "delay_after_ms": int(round(delay * 1000.0)),
        "agent_hint": hint,
        "messages": json.dumps(ms, ensure_ascii=False),
        "enable_thinking": False,
        "tools": None,
    }


def main():
    sess, rows, step = build()
    os.makedirs(OUT_DIR, exist_ok=True)
    for tag, with_cc in (("cc", True), ("nocc", False)):
        p = os.path.join(OUT_DIR, "ccvisit_real_%s_s%d_%s.jsonl" % (VARIANT, SHARD, tag))
        with open(p, "w") as fh:
            for delay, kind, ref, t in rows:
                fh.write(json.dumps(make_row(kind, ref, t, delay, with_cc), ensure_ascii=False) + "\n")
        print("wrote %s rows=%d" % (p, len(rows)))
    last = TURNS_MAX
    live = N_SESS * last // BLK
    pinned = N_PROTECT * last // BLK
    cblk = CHURN_TOK // BLK
    print("\nshard %d/%d sessions=%d protected=%d turns=%d final=%d tok"
          % (SHARD, SHARDS, N_SESS, N_PROTECT, TURNS, last))
    print("live context = %d blocks; pinned by protection = %d blocks"
          % (live, pinned))
    print("oversubscription = %.2fx pool 2031 (nothing is evicted below 1.0)" % (live / 2031.0))
    print("pinned = %.1f%% of pool; free after pinning = %d blocks"
          % (100.0 * pinned / 2031.0, 2031 - pinned))
    print("churn %d x %d tok = %d blocks each, in-flight ~%.1f -> %.0f blocks"
          % (N_CHURN, CHURN_TOK, cblk, max(1.0, prefill_s(CHURN_TOK) / CHURN_GAP),
             cblk * max(1.0, prefill_s(CHURN_TOK) / CHURN_GAP)))
    print("guard: pinned + in-flight = %d (pool 2031, %.0f%%)"
          % (pinned + cblk * max(1.0, prefill_s(CHURN_TOK) / CHURN_GAP),
             100.0 * (pinned + cblk * max(1.0, prefill_s(CHURN_TOK) / CHURN_GAP)) / 2031.0))
    print("churn step = %.2fs, requests=%d" % (step, N_CHURN))


if __name__ == "__main__":
    main()
