#!/usr/bin/env python3
"""ais_bench capture CLI: run a real harness against a recording proxy and
persist the captured requests as a model_io directory that the Agent Hint v2
builder consumes.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

from .batch import run_batch
from .model_io import convert as model_io_convert
from .recorder import Recorder
from .v2_builder import build as capture_v2_build
from .validate import validate as validate_dataset


def cmd_record(args) -> int:
    rec = Recorder(args.upstream_host, args.upstream_port, args.upstream_model, args.log, args.model_id)
    os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
    print(f"recorder :{args.listen_port} -> {args.upstream_host}:{args.upstream_port} log={args.log}", flush=True)
    rec.serve(args.listen_host, args.listen_port)
    return 0


def cmd_to_model_io(args) -> int:
    ns = argparse.Namespace(
        input=args.input, out=args.out, hints=args.hints,
        case_id=args.case_id, session_id=args.session_id,
    )
    return model_io_convert(ns)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cmd_run(args) -> int:
    port = args.listen_port or _free_port()
    log = os.path.abspath(os.path.join(args.out, "requests.jsonl"))
    os.makedirs(args.out, exist_ok=True)
    rec = Recorder(args.upstream_host, args.upstream_port, args.upstream_model, log, args.model_id)
    server = ThreadingHTTPServer((args.listen_host, port), rec.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{port}/v1"
    print(f"[capture] recorder on {endpoint}; harness cmd: {args.harness_cmd}", flush=True)
    env = os.environ.copy()
    env.update({
        "CAPTURE_ENDPOINT": endpoint,
        "CAPTURE_LOG": log,
        "HINTS_MODE": args.hints,
        "CAPTURE_RUN_ID": args.run_id or os.path.basename(os.path.abspath(args.out)),
    })
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    try:
        rc = subprocess.run(args.harness_cmd, shell=True, env=env).returncode
    finally:
        server.shutdown()
        server.server_close()
    print(f"[capture] harness exit={rc}; requests={log}", flush=True)
    if args.build_model_io:
        ns = argparse.Namespace(
            input=log, out=os.path.join(args.out, "model_io"), hints=args.hints,
            case_id=args.case_id, session_id=args.run_id,
        )
        model_io_convert(ns)
    return rc


def cmd_build(args) -> int:
    if args.builder == "capture":
        ns = argparse.Namespace(
            input_dir=args.input_dir, output=args.output, report=None,
            default_max_out_len=args.default_max_out_len,
            completion_token_cap=args.completion_token_cap,
            state_mode=args.state_mode or "running",
        )
        return capture_v2_build(ns)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    builder = os.path.join(here, "datasets", "agent", "build_agent_dataset_v2_from_model_io.py")
    cmd = [sys.executable, builder, "--input-dir", args.input_dir, "--output", args.output]
    for flag in ("state_mode", "session_limit", "max_records", "completion_token_cap",
                 "max_context", "context_reserve"):
        value = getattr(args, flag, None)
        if value is not None:
            cmd += [f"--{flag.replace('_', '-')}", str(value)]
    if args.exclude_compressor:
        cmd.append("--exclude-compressor")
    return subprocess.run(cmd).returncode


def cmd_batch(args) -> int:
    return run_batch(args)


def cmd_validate(args) -> int:
    return validate_dataset(args.dataset, args.hints)


def main() -> int:
    ap = argparse.ArgumentParser(prog="ais_bench.capture")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="run the recording proxy")
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=9999)
    p.add_argument("--upstream-host", required=True)
    p.add_argument("--upstream-port", type=int, required=True)
    p.add_argument("--upstream-model", default="")
    p.add_argument("--model-id", default="capture-model")
    p.add_argument("--log", required=True)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("to-model-io", help="convert capture JSONL to model_io/*.json")
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--hints", choices=["on", "off"], default="on")
    p.add_argument("--case-id", default=None)
    p.add_argument("--session-id", default=None)
    p.set_defaults(func=cmd_to_model_io)

    p = sub.add_parser("run", help="start recorder, run a harness command, persist model_io")
    p.add_argument("--harness-cmd", required=True)
    p.add_argument("--hints", choices=["on", "off"], default="on")
    p.add_argument("--out", required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--upstream-host", required=True)
    p.add_argument("--upstream-port", type=int, required=True)
    p.add_argument("--upstream-model", default="")
    p.add_argument("--model-id", default="capture-model")
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-port", type=int, default=0)
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--case-id", default=None)
    p.add_argument("--build-model-io", action="store_true", default=True)
    p.add_argument("--no-build-model-io", dest="build_model_io", action="store_false")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("build", help="build an Agent Hint v2 dataset from a model_io directory")
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--builder", choices=["capture", "jiuwen"], default="capture",
                   help="capture: preserve full harness hints; jiuwen: legacy model_io converter")
    p.add_argument("--default-max-out-len", type=int, default=256)
    p.add_argument("--state-mode", default=None)
    p.add_argument("--session-limit", type=int, default=None)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--completion-token-cap", type=int, default=256)
    p.add_argument("--max-context", type=int, default=None)
    p.add_argument("--context-reserve", type=int, default=None)
    p.add_argument("--exclude-compressor", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("batch", help="run a task list through a harness and build one v2 dataset")
    p.add_argument("--tasks", required=True, help="JSONL: {task_id, prompt, turns?}")
    p.add_argument("--harness-cmd", required=True)
    p.add_argument("--hints", choices=["on", "off"], default="on")
    p.add_argument("--out", required=True)
    p.add_argument("--max-tasks", type=int, default=0)
    p.add_argument("--turns", type=int, default=1)
    p.add_argument("--upstream-host", required=True)
    p.add_argument("--upstream-port", type=int, required=True)
    p.add_argument("--upstream-model", default="")
    p.add_argument("--model-id", default="capture-model")
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-port", type=int, default=0,
                   help="fixed port for harnesses whose provider endpoint is configured out of band (Dify)")
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--default-max-out-len", type=int, default=256)
    p.add_argument("--completion-token-cap", type=int, default=256)
    p.add_argument("--allow-failures", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("validate", help="validate a captured Agent Hint v2 dataset")
    p.add_argument("--dataset", required=True)
    p.add_argument("--hints", choices=["on", "off"], default="on")
    p.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
