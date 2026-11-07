#!/usr/bin/env python3
"""Build a DSH task manifest from a SWE-bench JSONL + offline snapshot manifest.

Example:
    python -m ais_bench.capture.examples.make_swebench_manifest \
      --dataset /home/t00887700/benchmark-data/swebench_verified_50_offline/swebench_verified_50_seed722.jsonl \
      --offline-manifest /home/t00887700/benchmark-data/swebench_verified_50_offline/offline_manifest.json \
      --output /home/t00887700/benchmark-data/agent_capture_dsh/tasks50.jsonl \
      --limit 5
"""
from __future__ import annotations

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--offline-manifest", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    manifest = json.load(open(args.offline_manifest, encoding="utf-8"))
    zips = {
        str(item.get("instance_id")): item.get("path")
        for item in manifest.get("snapshots", [])
        if item.get("instance_id") and item.get("path")
    }

    rows = []
    with open(args.dataset, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            task = json.loads(line)
            instance_id = str(task.get("instance_id") or "")
            zip_path = zips.get(instance_id)
            if not zip_path:
                continue
            task["workspace_zip"] = zip_path
            rows.append(task)
            if args.limit is not None and len(rows) >= args.limit:
                break

    with open(args.output, "w", encoding="utf-8") as f:
        for task in rows:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} tasks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
