#!/usr/bin/env python3
"""Batch harness capture: run a task list through a harness command, record
each task, convert to model_io, build one Agent Hint v2 dataset, and write a
manifest.

For real SWE-bench style tasks the runner can optionally extract a per-task
``workspace_zip`` snapshot, create a git baseline, and expose the workspace to
the harness through ``CAPTURE_WORKSPACE``. This keeps the workspace
provisioning inside the capture pipeline instead of maintaining a second
out-of-tree runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer

from .model_io import convert as model_io_convert
from .recorder import Recorder
from .v2_builder import build as v2_build

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_PROMPT_PREFIX = (
    "You are working in the current repository. Resolve the following issue. "
    "Inspect the code, make the minimal necessary change, and run the relevant "
    "tests when possible. Do not ask for confirmation.\n\n"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sanitize_name(value: str) -> str:
    return str(value).replace("/", "__").replace("\\", "__").replace(" ", "_")


def _task_id_of(task: dict, index: int, args) -> str:
    field = getattr(args, "case_id_field", None) if args else None
    if field and task.get(field) is not None:
        return str(task[field])
    for key in ("task_id", "instance_id"):
        if task.get(key) is not None:
            return str(task[key])
    return f"t{index:04d}"


def _prompt_of(task: dict, args) -> str:
    field = getattr(args, "prompt_field", None) if args else None
    candidates = []
    if field:
        candidates.append(field)
    candidates.extend(["prompt", "problem_statement"])
    source = None
    value = None
    for key in candidates:
        candidate = task.get(key)
        if isinstance(candidate, str) and candidate.strip():
            source = key
            value = candidate
            break
    if value is None:
        raise ValueError(f"task {task.get('task_id')} has no prompt/problem_statement")
    if getattr(args, "no_prompt_prefix", False):
        return value
    prefix = getattr(args, "prompt_prefix", None) if args else None
    if prefix is None:
        prefix = DEFAULT_PROMPT_PREFIX if source != "prompt" else ""
    return (prefix or "") + value


def read_tasks(path: str, args=None) -> list[dict]:
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
            task = dict(obj)
            task["task_id"] = _task_id_of(task, i, args)
            task["prompt"] = _prompt_of(task, args)
            tasks.append(task)
    return tasks


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_zip(zip_path: str, dest: str) -> None:
    """Extract a zip archive while rejecting path traversal entries."""
    dest_real = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = os.path.realpath(os.path.join(dest_real, member.filename))
            if os.path.commonpath([dest_real, target]) != dest_real:
                raise ValueError(
                    f"unsafe path in workspace zip: {member.filename!r}"
                )
        archive.extractall(dest)


def _workspace_marker(task_dir: str) -> str:
    return os.path.join(task_dir, ".capture_workspace.json")


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _init_git_baseline(path: str):
    if not os.path.isdir(os.path.join(path, ".git")):
        subprocess.run(["git", "init", "-q"], cwd=path, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "capture@localhost"], cwd=path,
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "capture"], cwd=path,
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "add", "-A"], cwd=path, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-qm", "capture baseline"], cwd=path,
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def prepare_workspace(task: dict, args) -> dict:
    """Extract ``workspace_zip`` for a task and optionally create a git baseline."""
    field = getattr(args, "workspace_field", None) or "workspace_zip"
    zip_path = task.get(field) or task.get("workspace_zip")
    if not zip_path:
        return {"workspace": None, "workspace_zip": None, "git_commit": None}
    zip_path = str(zip_path)
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"workspace_zip not found: {zip_path}")
    workspaces_dir = getattr(args, "workspaces_dir", None) or os.path.join(args.out, "workspaces")
    task_dir = os.path.join(workspaces_dir, _sanitize_name(task["task_id"]))
    zip_sha256 = sha256(zip_path)
    marker = _workspace_marker(task_dir)
    marker_data = {}
    if os.path.exists(marker):
        try:
            marker_data = json.load(open(marker, encoding="utf-8"))
        except Exception:
            marker_data = {}
    if marker_data.get("zip_sha256") != zip_sha256:
        shutil.rmtree(task_dir, ignore_errors=True)
    os.makedirs(task_dir, exist_ok=True)
    if not os.listdir(task_dir):
        try:
            _safe_extract_zip(zip_path, task_dir)
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
    _write_json(marker, {"zip_path": zip_path, "zip_sha256": zip_sha256})
    entries = [name for name in os.listdir(task_dir) if not name.startswith(".")]
    workspace = task_dir
    if len(entries) == 1 and os.path.isdir(os.path.join(task_dir, entries[0])):
        workspace = os.path.join(task_dir, entries[0])
    git_commit = None
    if getattr(args, "init_git", False):
        git_commit = _init_git_baseline(workspace)
    return {"workspace": workspace, "workspace_zip": zip_path, "git_commit": git_commit}


def run_one(task: dict, args, run_dir: str, workspace_info: dict | None = None) -> dict:
    os.makedirs(run_dir, exist_ok=True)
    workspace_info = workspace_info or {"workspace": None, "workspace_zip": None, "git_commit": None}
    prompt_file = os.path.join(run_dir, "prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(task["prompt"])
    task_record = {
        "task_id": task["task_id"],
        "prompt_file": prompt_file,
        "workspace": workspace_info.get("workspace"),
        "workspace_zip": workspace_info.get("workspace_zip"),
        "git_commit": workspace_info.get("git_commit"),
    }
    _write_json(os.path.join(run_dir, "task.json"), task_record)

    port = args.listen_port or free_port()
    log = os.path.join(run_dir, "requests.jsonl")
    rec = Recorder(args.upstream_host, args.upstream_port, args.upstream_model, log,
                   args.model_id, forward_agent_status=getattr(args, "forward_agent_status", False))
    server = ThreadingHTTPServer((args.listen_host, port), rec.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{port}/v1"
    status_log = os.path.join(run_dir, "status_events.jsonl")
    env = os.environ.copy()
    env.update({
        "CAPTURE_ENDPOINT": endpoint,
        "CAPTURE_LOG": log,
        "CAPTURE_STATUS_LOG": status_log,
        "CAPTURE_RUN_ID": task["task_id"],
        "CAPTURE_TASK_ID": task["task_id"],
        "CAPTURE_PROMPT": task["prompt"],
        "CAPTURE_PROMPT_FILE": prompt_file,
        "CAPTURE_TURNS": str(task.get("turns", args.turns)),
        "HINTS_MODE": args.hints,
    })
    if workspace_info.get("workspace"):
        env["CAPTURE_WORKSPACE"] = str(workspace_info["workspace"])
    for key, attr in (
        ("CAPTURE_MAX_TOKENS", "max_tokens"),
        ("CAPTURE_TIMEOUT_S", "timeout_s"),
        ("CAPTURE_THINKING", "thinking"),
        ("CAPTURE_REASONING_EFFORT", "reasoning_effort"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            env[key] = str(value)
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    t0 = time.time()
    timeout = getattr(args, "timeout_s", 0) or None
    proc = None
    try:
        proc = subprocess.Popen(
            args.harness_cmd, shell=True, env=env, cwd=run_dir,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            rc = 124
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        server.shutdown()
        server.server_close()
    duration = round(time.time() - t0, 2)
    mi_out = os.path.join(run_dir, "model_io")
    stats = {}
    if os.path.exists(log) and os.path.getsize(log) > 0:
        ns = argparse.Namespace(input=log, out=mi_out, hints=args.hints,
                                case_id=task["task_id"],
                                session_id=task["task_id"] if args.hints == "off" else None,
                                status_events=status_log if os.path.exists(status_log) else None)
        model_io_convert(ns)
        report_path = os.path.join(mi_out, "capture.report.json")
        if os.path.exists(report_path):
            stats = json.load(open(report_path, encoding="utf-8"))
    result = {
        "task_id": task["task_id"],
        "returncode": rc,
        "duration_s": duration,
        "requests_log": log,
        "status_events": status_log if os.path.exists(status_log) else None,
        "model_io": mi_out,
        "capture": stats,
        "workspace": workspace_info.get("workspace"),
        "workspace_zip": workspace_info.get("workspace_zip"),
        "git_commit": workspace_info.get("git_commit"),
    }
    _write_json(os.path.join(run_dir, "task.json"), {**task_record, **result})
    return result


def _load_existing_run(task: dict, run_dir: str) -> dict | None:
    log = os.path.join(run_dir, "requests.jsonl")
    mi_out = os.path.join(run_dir, "model_io")
    if not (os.path.exists(log) and os.path.isdir(mi_out) and os.listdir(mi_out)):
        return None
    status_log = os.path.join(run_dir, "status_events.jsonl")
    report_path = os.path.join(mi_out, "capture.report.json")
    stats = {}
    if os.path.exists(report_path):
        try:
            stats = json.load(open(report_path, encoding="utf-8"))
        except Exception:
            stats = {}
    task_record = {}
    task_json = os.path.join(run_dir, "task.json")
    if os.path.exists(task_json):
        try:
            task_record = json.load(open(task_json, encoding="utf-8"))
        except Exception:
            task_record = {}
    if "returncode" not in task_record:
        return None
    try:
        returncode = int(task_record["returncode"])
    except (TypeError, ValueError):
        return None
    return {"task_id": task["task_id"], "returncode": returncode,
            "duration_s": task_record.get("duration_s", 0),
            "requests_log": log,
            "status_events": status_log if os.path.exists(status_log) else None,
            "model_io": mi_out, "capture": stats,
            "workspace": task_record.get("workspace"),
            "workspace_zip": task_record.get("workspace_zip"),
            "git_commit": task_record.get("git_commit")}


def _merge_model_io(runs: list[dict], dest: str) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for res in runs:
        names = os.listdir(res["model_io"]) if os.path.isdir(res["model_io"]) else []
        for name in names:
            if name.endswith(".json") and name != "capture.report.json":
                src = os.path.join(res["model_io"], name)
                dst = os.path.join(dest, f"{_sanitize_name(res['task_id'])}__{name}")
                shutil.copyfile(src, dst)


def _merge_status(runs: list[dict], dest: str) -> str | None:
    paths = [res["status_events"] for res in runs if res.get("status_events")]
    if not paths:
        return None
    with open(dest, "w", encoding="utf-8") as out:
        for path in paths:
            with open(path, encoding="utf-8") as src:
                for line in src:
                    if line.strip():
                        out.write(line if line.endswith("\n") else line + "\n")
    return dest if os.path.getsize(dest) > 0 else None


def _build_with_python(args, merged: str, status_path: str | None, output: str) -> dict | None:
    cmd = [args.build_python, "-m", "ais_bench.capture.v2_builder",
           "--input-dir", merged, "--output", output,
           "--default-max-out-len", str(args.default_max_out_len),
           "--completion-token-cap", str(args.completion_token_cap),
           "--state-mode", "completed-finalize" if status_path else "running"]
    if getattr(args, "replay_pause_events", False):
        cmd.append("--replay-pause-events")
    if status_path:
        cmd += ["--status-events", status_path]
    if getattr(args, "pause_delay_min_ms", 0):
        cmd += ["--pause-delay-min-ms", str(args.pause_delay_min_ms)]
    if getattr(args, "tokenizer", None):
        cmd += ["--tokenizer", args.tokenizer]
    if getattr(args, "max_context", None):
        cmd += ["--max-context", str(args.max_context)]
    if getattr(args, "skip_tokenizer_check", False):
        cmd += ["--skip-tokenizer-check"]
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"build_python failed with exit {proc.returncode}")
    report_path = output + ".report.json"
    if os.path.exists(report_path):
        return json.load(open(report_path, encoding="utf-8"))
    return None


def _build_dataset(args, merged: str, status_path: str | None, output: str) -> dict | None:
    if getattr(args, "build_python", None):
        return _build_with_python(args, merged, status_path, output)
    ns = argparse.Namespace(
        input_dir=merged, output=output, report=None,
        default_max_out_len=args.default_max_out_len,
        completion_token_cap=args.completion_token_cap,
        state_mode="completed-finalize" if status_path else "running",
        status_events=status_path,
        tokenizer=getattr(args, "tokenizer", None),
        max_context=getattr(args, "max_context", None),
        skip_tokenizer_check=getattr(args, "skip_tokenizer_check", False),
        replay_pause_events=getattr(args, "replay_pause_events", False),
        pause_delay_min_ms=getattr(args, "pause_delay_min_ms", 0),
    )
    v2_build(ns)
    report_path = output + ".report.json"
    if os.path.exists(report_path):
        return json.load(open(report_path, encoding="utf-8"))
    return None


def run_batch(args) -> int:
    tasks = read_tasks(args.tasks, args)
    limit = getattr(args, "task_limit", 0) or getattr(args, "max_tasks", 0)
    if limit:
        tasks = tasks[:limit]
    os.makedirs(args.out, exist_ok=True)
    runs_dir = os.path.join(args.out, "runs")
    merged = os.path.join(args.out, "model_io")
    os.makedirs(merged, exist_ok=True)
    started = time.time()
    runs = []
    failures = 0
    for task in tasks:
        run_dir = os.path.join(runs_dir, _sanitize_name(task["task_id"]))
        print(f"[batch] task={task['task_id']} dir={run_dir}", flush=True)
        res = None
        if getattr(args, "skip_existing", False):
            res = _load_existing_run(task, run_dir)
            if res is not None:
                print(f"[batch] task={task['task_id']} skipped (existing model_io)", flush=True)
        if res is None:
            workspace_info = prepare_workspace(task, args)
            res = run_one(task, args, run_dir, workspace_info)
        runs.append(res)
        if res["returncode"] != 0:
            failures += 1
        print(f"[batch] task={task['task_id']} exit={res['returncode']} "
              f"kept={res['capture'].get('records_kept')} "
              f"sessions={res['capture'].get('sessions')}", flush=True)

    _merge_model_io(runs, merged)
    merged_status = _merge_status(runs, os.path.join(args.out, "status_events.jsonl"))
    builder_report = None
    if os.listdir(merged):
        builder_report = _build_dataset(args, merged, merged_status,
                                        os.path.join(args.out, "dataset_v2.jsonl"))

    success_runs = [res for res in runs if res["returncode"] == 0]
    success_merged = os.path.join(args.out, "model_io_success")
    success_status = None
    success_report = None
    success_dataset = None
    if getattr(args, "only_successful", False) and success_runs:
        _merge_model_io(success_runs, success_merged)
        success_status = _merge_status(success_runs, os.path.join(args.out, "status_events_success.jsonl"))
        if os.listdir(success_merged):
            success_dataset = os.path.join(args.out, "dataset_v2_success.jsonl")
            success_report = _build_dataset(args, success_merged, success_status, success_dataset)

    manifest = {
        "schema": "harness-capture-manifest-v1",
        "harness_cmd": args.harness_cmd,
        "hints": args.hints,
        "upstream": {"host": args.upstream_host, "port": args.upstream_port, "model": args.upstream_model},
        "tasks_file": os.path.abspath(args.tasks),
        "tasks_sha256": sha256(args.tasks),
        "task_count": len(tasks),
        "failure_count": failures,
        "only_successful": bool(getattr(args, "only_successful", False)),
        "started_at": started,
        "finished_at": time.time(),
        "runs": runs,
        "dataset": os.path.abspath(os.path.join(args.out, "dataset_v2.jsonl"))
        if os.path.exists(os.path.join(args.out, "dataset_v2.jsonl")) else None,
        "dataset_sha256": sha256(os.path.join(args.out, "dataset_v2.jsonl"))
        if os.path.exists(os.path.join(args.out, "dataset_v2.jsonl")) else None,
        "dataset_success": os.path.abspath(success_dataset) if success_dataset else None,
        "status_events": os.path.abspath(merged_status) if merged_status else None,
        "builder_report": builder_report,
        "builder_report_success": success_report,
    }
    manifest_path = os.path.join(args.out, "dataset.manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps({"manifest": manifest_path, "dataset": manifest["dataset"],
                      "dataset_success": manifest["dataset_success"],
                      "tasks": len(tasks), "failures": failures,
                      "records": (builder_report or {}).get("output_records"),
                      "sessions": (builder_report or {}).get("sessions")},
                     ensure_ascii=False, indent=2))
    return 0 if (failures == 0 or args.allow_failures) else 1
