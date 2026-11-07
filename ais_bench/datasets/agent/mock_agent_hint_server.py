#!/usr/bin/env python3
"""Minimal OpenAI-compatible mock for Agent Hint send-side conformance tests.

It intentionally does not run a model. It records every received
``X-Agent-Hint`` header and ``/agent-status`` payload, then answers with a tiny
streaming chat completion so AISBench can complete quickly without a GPU.

Env:
    MOCK_PORT           listen port (default 18080)
    MOCK_RECORD_PATH    received-events JSONL path (default /tmp/mock_received.jsonl)
    MOCK_SERVED_NAME    served model name (default kvh)
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from aiohttp import web

PORT = int(os.getenv("MOCK_PORT", "18080"))
RECORD_PATH = os.getenv("MOCK_RECORD_PATH", "/tmp/mock_received.jsonl")
SERVED_NAME = os.getenv("MOCK_SERVED_NAME", "kvh")


def _record(kind: str, **fields: Any) -> None:
    record = {"kind": kind, "ts": time.time()}
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(RECORD_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _decode_header(value: str | None) -> Any:
    if not value:
        return None
    try:
        payload = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _session_state(hint: Any) -> tuple[str, str]:
    if not isinstance(hint, dict):
        return "", ""
    session = hint.get("session") if isinstance(hint.get("session"), dict) else {}
    status = hint.get("agent_status") if isinstance(hint.get("agent_status"), dict) else {}
    return str(session.get("session_id", "")), str(status.get("state", ""))


async def models(_request: web.Request) -> web.Response:
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": SERVED_NAME,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "mock",
                    "root": SERVED_NAME,
                    "parent": None,
                    "max_model_len": 32768,
                    "permission": [],
                }
            ],
        }
    )


async def chat_completions(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        print(f"MOCK chat json error: {exc!r}", flush=True)
        raise
    raw_header = request.headers.get("X-Agent-Hint")
    print(f"MOCK chat path={request.path} has_header={bool(raw_header)}", flush=True)
    hint = _decode_header(raw_header)
    session_id, state = _session_state(hint)
    body_has_agent_hint = isinstance(body, dict) and "agent_hint" in body
    stream = bool(body.get("stream")) if isinstance(body, dict) else False
    _record(
        "received_request",
        has_header=bool(raw_header),
        body_has_agent_hint=body_has_agent_hint,
        stream=stream,
    )
    if raw_header:
        _record(
            "received_header",
            session_id=session_id,
            state=state,
            header=raw_header,
            hint=hint,
            body_has_agent_hint=body_has_agent_hint,
            stream=stream,
        )

    created = int(time.time())
    if not body.get("stream"):
        return web.json_response(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": created,
                "model": SERVED_NAME,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await response.prepare(request)
    base = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": created,
        "model": SERVED_NAME,
    }
    first = dict(base)
    first["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]
    last = dict(base)
    last["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    last["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    await response.write(f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8"))
    await response.write(f"data: {json.dumps(last, ensure_ascii=False)}\n\n".encode("utf-8"))
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def agent_status(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    session = payload.get("session") if isinstance(payload, dict) else {}
    status = payload.get("agent_status") if isinstance(payload, dict) else {}
    session_id = session.get("session_id") if isinstance(session, dict) else ""
    state = status.get("state") if isinstance(status, dict) else ""
    _record("received_status", session_id=session_id, state=state, payload=payload)
    return web.json_response(
        {
            "status": "ok",
            "applied": True,
            "reason": "applied",
            "state": state,
            "previous_state": None,
        }
    )


def main() -> None:
    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/agent-status", agent_status)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
