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


def parse_json_body(raw: bytes):
    """Decode a request body, tolerating raw control characters.

    Some harness/tool-result payloads contain literal control characters inside
    JSON strings. ``json.loads(..., strict=False)`` accepts them; re-serializing
    with ``json.dumps`` escapes them, so the upstream receives valid JSON.
    Returns (parsed_or_None, mode) where mode is strict/relaxed/invalid/empty.
    """
    if not raw:
        return None, "empty"
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text), "strict"
    except Exception:
        pass
    try:
        return json.loads(text, strict=False), "relaxed"
    except Exception:
        return None, "invalid"


def _usage_from_payload(payload: str):
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) else None


def feed_usage(buffer: bytes, chunk: bytes):
    """Consume an SSE byte stream and return (latest_usage, remaining_buffer).

    Chunks are arbitrary: a ``data:`` line can be split across reads, and one
    read can contain several SSE lines. Keep the partial tail so the caller can
    feed the next chunk. ``\r\n`` is handled by strip().
    """
    buffer += chunk
    usage = None
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data:"):
            continue
        found = _usage_from_payload(text[5:].strip())
        if found is not None:
            usage = found
    return usage, buffer


class Recorder:
    def __init__(self, upstream_host, upstream_port, upstream_model, log_path, model_id,
                 forward_agent_status=False):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_model = upstream_model
        self.log_path = log_path
        self.model_id = model_id
        self.forward_agent_status = bool(forward_agent_status)

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
                original, body_parse = parse_json_body(raw)
                body = dict(original) if isinstance(original, dict) else None
                if body is not None:
                    body["model"] = rec.upstream_model
                    if body.get("stream") and "stream_options" not in body:
                        body["stream_options"] = {"include_usage": True}
                new_raw = json.dumps(body).encode() if body is not None else raw

                if self.path.rstrip("/").endswith("/agent-status"):
                    if not rec.forward_agent_status:
                        rec.log({"ts": time.time(), "path": self.path, "headers": headers,
                                 "body": original, "kind": "agent-status", "status": 200,
                                 "forwarded": False})
                        self._json(200, {})
                        return
                    fwd = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
                    fwd["Host"] = f"{rec.upstream_host}:{rec.upstream_port}"
                    fwd["Content-Length"] = str(len(raw))
                    t0 = time.time()
                    try:
                        conn = http.client.HTTPConnection(
                            rec.upstream_host, rec.upstream_port, timeout=30
                        )
                        conn.request("POST", self.path, body=raw, headers=fwd)
                        resp = conn.getresponse()
                        resp_body = resp.read()
                        upstream_status = resp.status
                        conn.close()
                        rec.log({
                            "ts": time.time(), "path": self.path, "headers": headers,
                            "body": original, "kind": "agent-status",
                            "forwarded": True, "upstream_status": upstream_status,
                            "response": resp_body.decode("utf-8", errors="replace")[:2000],
                            "duration_ms": round((time.time() - t0) * 1000, 1),
                        })
                        self.send_response(upstream_status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(resp_body)
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {exc}"
                        rec.log({
                            "ts": time.time(), "path": self.path, "headers": headers,
                            "body": original, "kind": "agent-status",
                            "forwarded": True, "error": err,
                            "duration_ms": round((time.time() - t0) * 1000, 1),
                        })
                        self._json(502, {"error": err})
                    return

                fwd = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
                fwd["Host"] = f"{rec.upstream_host}:{rec.upstream_port}"
                fwd["Content-Length"] = str(len(new_raw))

                t0 = time.time()
                status = None
                err = ""
                usage = None
                usage_buf = b""
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
                        found, usage_buf = feed_usage(usage_buf, chunk)
                        if found is not None:
                            usage = found
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    conn.close()
                    if usage is None:
                        tail = usage_buf.decode("utf-8", errors="replace").strip()
                        if tail:
                            try:
                                found = _usage_from_payload(tail)
                            except Exception:
                                found = None
                            if found is not None:
                                usage = found
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
                        "body_parse": body_parse,
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
