#!/usr/bin/env python3
"""Batch harness capture: run a task list through a harness command, record
each task, convert to model_io, build one Agent Hint v2 dataset, and write a
manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

from .model_io import convert as model_io_convert
from .recorder import Recorder
from .v2_builder import build as v2_build


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_tasks(path: str) -> list[dict]:
    tasks = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                obj = {"task_id": f"t{i:04d}", "prompt": line}
            if isinstance(obj, str):
                obj = {"task_id": f"t{i:04d}", "prompt": obj}
            obj.setdefault("task_id", f"t{i:04d}")
            if "prompt" not in obj:
                raise ValueError(f"task {obj['task_id']} has no prompt")
            tasks.append(obj)
    return tasks


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_one(task: dict, args, run_dir: str) -> dict:
    os.makedirs(run_dir, exist_ok=True)
    port = args.listen_port or free_port()
    log = os.path.join(run_dir, "requests.jsonl")
    rec = Recorder(args.upstream_host, args.upstream_port, args.upstream_model, log, args.model_id)
    server = ThreadingHTTPServer((args.listen_host, port), rec.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{port}/v1"
    env = os.environ.copy()
    env.update({
        "CAPTURE_ENDPOINT": endpoint,
        "CAPTURE_LOG": log,
        "CAPTURE_RUN_ID": task["task_id"],
        "CAPTURE_TASK_ID": task["task_id"],
        "CAPTURE_PROMPT": task["prompt"],
        "CAPTURE_TURNS": str(task.get("turns", args.turns)),
        "HINTS_MODE": args.hints,
    })
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    t0 = time.time()
    try:
        rc = subprocess.run(args.harness_cmd, shell=True, env=env, cwd=run_dir).returncode
    finally:
        server.shutdown()
        server.server_close()
    duration = round(time.time() - t0, 2)
    mi_out = os.path.join(run_dir, "model_io")
    stats = {}
    if os.path.exists(log) and os.path.getsize(log) > 0:
        ns = argparse.Namespace(input=log, out=mi_out, hints=args.hints,
                                case_id=task["task_id"],
                                session_id=task["task_id"] if args.hints == "off" else None)
        model_io_convert(ns)
        report_path = os.path.join(mi_out, "capture.report.json")
        if os.path.exists(report_path):
            stats = json.load(open(report_path, encoding="utf-8"))
    return {"task_id": task["task_id"], "returncode": rc, "duration_s": duration,
            "requests_log": log, "model_io": mi_out, "capture": stats}


def run_batch(args) -> int:
    tasks = read_tasks(args.tasks)
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]
    os.makedirs(args.out, exist_ok=True)
    runs_dir = os.path.join(args.out, "runs")
    merged = os.path.join(args.out, "model_io")
    os.makedirs(merged, exist_ok=True)
    started = time.time()
    runs = []
    failures = 0
    for task in tasks:
        run_dir = os.path.join(runs_dir, task["task_id"])
        print(f"[batch] task={task['task_id']} dir={run_dir}", flush=True)
        res = run_one(task, args, run_dir)
        runs.append(res)
        if res["returncode"] != 0:
            failures += 1
        names = os.listdir(res["model_io"]) if os.path.isdir(res["model_io"]) else []
        for name in names:
            if name.endswith(".json") and name != "capture.report.json":
                src = os.path.join(res["model_io"], name)
                dst = os.path.join(merged, f"{task['task_id']}__{name}")
                shutil.copyfile(src, dst)
        print(f"[batch] task={task['task_id']} exit={res['returncode']} "
              f"kept={res['capture'].get('records_kept')} "
              f"sessions={res['capture'].get('sessions')}", flush=True)

    dataset = os.path.join(args.out, "dataset_v2.jsonl")
    builder_report = None
    if os.listdir(merged):
        ns = argparse.Namespace(input_dir=merged, output=dataset, report=None,
                                default_max_out_len=args.default_max_out_len,
                                completion_token_cap=args.completion_token_cap,
                                state_mode="running")
        v2_build(ns)
        rp = dataset + ".report.json"
        if os.path.exists(rp):
            builder_report = json.load(open(rp, encoding="utf-8"))

    manifest = {
        "schema": "harness-capture-manifest-v1",
        "harness_cmd": args.harness_cmd,
        "hints": args.hints,
        "upstream": {"host": args.upstream_host, "port": args.upstream_port, "model": args.upstream_model},
        "tasks_file": os.path.abspath(args.tasks),
        "tasks_sha256": sha256(args.tasks),
        "task_count": len(tasks),
        "failure_count": failures,
        "started_at": started,
        "finished_at": time.time(),
        "runs": runs,
        "dataset": os.path.abspath(dataset) if os.path.exists(dataset) else None,
        "dataset_sha256": sha256(dataset) if os.path.exists(dataset) else None,
        "builder_report": builder_report,
    }
    manifest_path = os.path.join(args.out, "dataset.manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps({"manifest": manifest_path, "dataset": manifest["dataset"],
                      "tasks": len(tasks), "failures": failures,
                      "records": (builder_report or {}).get("output_records"),
                      "sessions": (builder_report or {}).get("sessions")},
                     ensure_ascii=False, indent=2))
    return 0 if (failures == 0 or args.allow_failures) else 1
