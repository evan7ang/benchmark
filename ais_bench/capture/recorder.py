#!/usr/bin/env python3
"""Recording proxy for harness capture.

Sits between a harness (DSH / Dify / ...) and an OpenAI-compatible upstream
(Motor or a plain vLLM), logs every request (headers incl. X-Agent-Hint, body,
usage, timing), rewrites the model name to the upstream served model, and
streams the response back unchanged.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()


def decode_hint(value):
    if not value:
        return None
    try:
        pad = "=" * ((4 - len(value) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(value + pad).decode("utf-8"))
    except Exception:
        return None


def extract_usage(chunk: bytes):
    text = chunk.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) else None


class Recorder:
    def __init__(self, upstream_host, upstream_port, upstream_model, log_path, model_id):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_model = upstream_model
        self.log_path = log_path
        self.model_id = model_id

    def log(self, record):
        line = json.dumps(record, ensure_ascii=False)
        with LOCK:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def handler(self):
        rec = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "data": [{"id": rec.model_id, "object": "model"}]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                try:
                    original = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    original = None
                body = dict(original) if isinstance(original, dict) else None
                if body is not None:
                    body["model"] = rec.upstream_model
                    if body.get("stream") and "stream_options" not in body:
                        body["stream_options"] = {"include_usage": True}
                new_raw = json.dumps(body).encode() if body is not None else raw

                if self.path.rstrip("/").endswith("/agent-status"):
                    rec.log({"ts": time.time(), "path": self.path, "headers": headers,
                             "body": original, "kind": "agent-status", "status": 200})
                    self._json(200, {})
                    return

                fwd = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
                fwd["Host"] = f"{rec.upstream_host}:{rec.upstream_port}"
                fwd["Content-Length"] = str(len(new_raw))

                t0 = time.time()
                status = None
                err = ""
                usage = None
                try:
                    conn = http.client.HTTPConnection(rec.upstream_host, rec.upstream_port, timeout=900)
                    conn.request("POST", self.path, body=new_raw, headers=fwd)
                    resp = conn.getresponse()
                    status = resp.status
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() in ("transfer-encoding", "content-length", "connection", "content-encoding"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        found = extract_usage(chunk)
                        if found:
                            usage = found
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    conn.close()
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    if status is None:
                        try:
                            self._json(502, {"error": err})
                        except Exception:
                            pass
                finally:
                    rec.log({
                        "ts": time.time(),
                        "path": self.path,
                        "headers": headers,
                        "body": original,
                        "model_sent": (original or {}).get("model") if isinstance(original, dict) else None,
                        "model_upstream": rec.upstream_model,
                        "hint": decode_hint(headers.get("x-agent-hint")),
                        "usage": usage,
                        "upstream_status": status,
                        "duration_ms": round((time.time() - t0) * 1000, 1),
                        "error": err,
                    })

        return Handler

    def serve(self, host, port):
        server = ThreadingHTTPServer((host, port), self.handler())
        server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenAI-compatible recording proxy for harness capture.")
    ap.add_argument("--listen-host", default=os.environ.get("CAPTURE_LISTEN_HOST", "0.0.0.0"))
    ap.add_argument("--listen-port", type=int, default=int(os.environ.get("CAPTURE_LISTEN_PORT", "9999")))
    ap.add_argument("--upstream-host", default=os.environ.get("CAPTURE_UPSTREAM_HOST", "127.0.0.1"))
    ap.add_argument("--upstream-port", type=int, default=int(os.environ.get("CAPTURE_UPSTREAM_PORT", "8000")))
    ap.add_argument("--upstream-model", default=os.environ.get("CAPTURE_UPSTREAM_MODEL", ""))
    ap.add_argument("--model-id", default=os.environ.get("CAPTURE_MODEL_ID", "capture-model"))
    ap.add_argument("--log", default=os.environ.get("CAPTURE_LOG", "capture.jsonl"))
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
    rec = Recorder(args.upstream_host, args.upstream_port, args.upstream_model, args.log, args.model_id)
    print(f"recorder :{args.listen_port} -> {args.upstream_host}:{args.upstream_port} "
          f"model={args.upstream_model or '(keep)'} log={args.log}", flush=True)
    rec.serve(args.listen_host, args.listen_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
