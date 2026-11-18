#!/usr/bin/env python3
"""Static package verification for KVC_manage."""
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "generated", "lpstd_large_v16_a4x16.jsonl")
EXPECTED_MD5 = "6529aa377d41f6af9da5a23f8dc23e82"


def main():
    rows = [json.loads(line) for line in open(DATA, encoding="utf-8") if line.strip()]
    sessions = {r["agent_hint"]["session"]["session_id"] for r in rows}
    victims = {s for s in sessions if "victim" in s}
    resume = [r for r in rows if r.get("question", "").endswith("-resume")]
    pause = [r for r in rows if r.get("question", "").endswith("-pause")]
    md5 = hashlib.md5(open(DATA, "rb").read()).hexdigest()
    assert len(rows) == 132, len(rows)
    assert len(sessions) == 20, len(sessions)
    assert len(victims) == 16, len(victims)
    assert len(resume) == 16, len(resume)
    assert len(pause) == 16, len(pause)
    assert md5 == EXPECTED_MD5, md5
    cfg = json.load(open(os.path.join(ROOT, "config", "scenario.json"), encoding="utf-8"))
    assert cfg["result"]["on"]["victim_hit_blocks"] == 65
    assert cfg["result"]["overall_mean_change"] == -0.263
    print("PASS rows=%d sessions=%d victims=%d resume=%d pause=%d md5=%s" % (
        len(rows), len(sessions), len(victims), len(resume), len(pause), md5))


if __name__ == "__main__":
    main()
