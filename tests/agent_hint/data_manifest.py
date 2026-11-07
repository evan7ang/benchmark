#!/usr/bin/env python3
"""Check and report the data assets used by Agent Hint tests.

The defaults match the local benchmark host, but every path can be relocated
with an environment variable. This script is intentionally dependency-free so
it can run before AISBench or pytest is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = os.environ.get("AGENT_HINT_DATA_DIR", "/home/t00887700/benchmark-data/agent_replay_v2")
CAPTURE_DIR = os.environ.get(
    "AGENT_HINT_CAPTURE_DIR", "/home/t00887700/benchmark-data/agent_capture_dsh/current"
)
LOCAL_DATA_DIR = os.environ.get(
    "AGENT_HINT_LOCAL_DATA_DIR", "/home/t00887700/kv-hint/.build/aisbench/data"
)
REPO_ROOT = Path(__file__).resolve().parents[2]

ASSETS: list[dict[str, Any]] = [
    {
        "name": "example_v2",
        "group": "core",
        "path": os.environ.get(
            "AGENT_HINT_EXAMPLE_V2", str(REPO_ROOT / "ais_bench/datasets/agent/example_v2.jsonl")
        ),
    },
    {"name": "swebench_full50_v2_running", "group": "core", "path": os.path.join(DATA_DIR, "swebench_full50_v2_running.jsonl")},
    {"name": "swebench_full50_v2_finalize", "group": "core", "path": os.path.join(DATA_DIR, "swebench_full50_v2_finalize.jsonl")},
    {"name": "swebench_full50_v2_pause_offload", "group": "core", "path": os.path.join(DATA_DIR, "swebench_full50_v2_pause_offload.jsonl")},
    {"name": "swebench_smoke_v2_running", "group": "core", "path": os.path.join(DATA_DIR, "swebench_smoke_v2_running.jsonl")},
    {"name": "swebench_smoke_v2_finalize", "group": "core", "path": os.path.join(DATA_DIR, "swebench_smoke_v2_finalize.jsonl")},
    {"name": "dsh_capture_v2", "group": "capture", "path": os.path.join(CAPTURE_DIR, "dataset_v2.jsonl")},
    {"name": "swebench_sat_kvc", "group": "local", "path": os.path.join(LOCAL_DATA_DIR, "swebench_sat_kvc.jsonl")},
    {"name": "swebench_sat_ramp", "group": "local", "path": os.path.join(LOCAL_DATA_DIR, "swebench_sat_ramp.jsonl")},
    {"name": "swebench_full50_v2_finalize_quick", "group": "local", "path": os.path.join(LOCAL_DATA_DIR, "swebench_full50_v2_finalize_quick.jsonl")},
    {"name": "swebench_smoke_v2_finalize_quick", "group": "local", "path": os.path.join(LOCAL_DATA_DIR, "swebench_smoke_v2_finalize_quick.jsonl")},
    {"name": "swebench_full50_v2_pause_offload_cap128", "group": "local", "path": os.path.join(LOCAL_DATA_DIR, "swebench_full50_v2_pause_offload_cap128.jsonl")},
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--required",
        default="core",
        help="comma-separated groups/names required to exist, or 'all' (default: core)",
    )
    parser.add_argument("--sha256", action="store_true", help="compute sha256 for present files")
    parser.add_argument("--report", default=None, help="write the report JSON to this path")
    args = parser.parse_args()

    required = {item.strip() for item in args.required.split(",") if item.strip()}
    require_all = "all" in required

    report = {
        "env": {
            "AGENT_HINT_DATA_DIR": DATA_DIR,
            "AGENT_HINT_CAPTURE_DIR": CAPTURE_DIR,
            "AGENT_HINT_LOCAL_DATA_DIR": LOCAL_DATA_DIR,
            "AGENT_HINT_EXAMPLE_V2": os.environ.get("AGENT_HINT_EXAMPLE_V2"),
        },
        "assets": [],
        "required": sorted(required),
    }
    missing_required: list[str] = []
    for asset in ASSETS:
        path = Path(asset["path"])
        present = path.exists()
        item = {
            "name": asset["name"],
            "group": asset["group"],
            "path": str(path),
            "present": present,
            "size": path.stat().st_size if present else None,
        }
        if present and args.sha256:
            item["sha256"] = sha256(path)
        report["assets"].append(item)
        if not present and (require_all or asset["name"] in required or asset["group"] in required):
            missing_required.append(asset["name"])

    report["ok"] = not missing_required
    report["missing_required"] = missing_required
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
