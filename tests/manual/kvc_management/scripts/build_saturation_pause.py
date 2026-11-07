#!/usr/bin/env python3
"""Turn the multi-session pause dataset into an early-pause saturation dataset.

Under a fully saturated engine the third turn of a session may take many
minutes, so a normal pause point does not fire within a short manual run. This
script moves the pause to the first turn of every session (and the resume to
the second turn), which lets the high-water gate and offload path trigger while
the engine is still saturated.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, OrderedDict


def session_of(row: dict) -> str:
    return str(((row.get("agent_hint") or {}).get("session") or {}).get("session_id") or "")


def state_of(row: dict) -> str | None:
    value = ((row.get("agent_hint") or {}).get("agent_status") or {}).get("state")
    return value.lower() if isinstance(value, str) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    data_dir = os.environ.get("DATA_DIR", ".")
    parser.add_argument(
        "--input",
        default=os.path.join(data_dir, "kvc_manual_pause_offload_quick.jsonl"),
    )

    parser.add_argument("--delay-ms", type=int, default=45000)
    parser.add_argument("--tag", default="", help="suffix inserted before .jsonl, e.g. _fast")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if not args.output:
        args.output = os.path.join(
            data_dir, f"kvc_manual_saturation_pause{args.tag}.jsonl"
        )

    rows = [json.loads(line) for line in open(args.input, encoding="utf-8") if line.strip()]
    grouped: "OrderedDict[str, list[dict]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(session_of(row), []).append(row)

    for items in grouped.values():
        if len(items) < 3:
            continue
        for row in items:
            if state_of(row) in ("pause", "suspend", "tool"):
                row["agent_hint"]["agent_status"]["state"] = "running"
                row["delay_after_ms"] = None
        items[0]["agent_hint"]["agent_status"]["state"] = "pause"
        items[0]["delay_after_ms"] = args.delay_ms
        items[1]["agent_hint"]["agent_status"]["state"] = "resume"
        items[1]["delay_after_ms"] = None

    with open(args.output, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    states = Counter(state_of(row) or "null" for row in rows)
    print(json.dumps({"rows": len(rows), "sessions": len(grouped), "states": dict(states)}))


if __name__ == "__main__":
    main()
