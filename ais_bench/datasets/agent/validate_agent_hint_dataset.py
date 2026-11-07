#!/usr/bin/env python3
"""Static conformance checks for Agent Hint v2 JSONL datasets.

This validator does not contact Motor or vLLM. It checks that a replayable
dataset is internally consistent and that every ``agent_hint`` field is within
the wire/protocol domain consumed by Motor.

Usage:
    python3 ais_bench/datasets/agent/validate_agent_hint_dataset.py \
        ais_bench/datasets/agent/example_v2.jsonl \
        /home/t00887700/benchmark-data/agent_replay_v2/swebench_smoke_v2_finalize.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ais_bench.benchmark.utils.agent_hint import (  # noqa: E402
    MAX_HEADER_CHARS,
    encode_agent_hint_header,
)

ALLOWED_TOP_LEVEL = {
    "session",
    "agent_status",
    "priority",
    "strict_priority",
    "cache_control",
    "prediction",
    "context_management",
    "session_control",
    "latency_control",
    "priority_control",
}
BODY_ONLY_TOP_LEVEL = {"context_management", "session_control", "latency_control", "priority_control"}
ALLOWED_STATES = {"running", "tool", "pause", "suspend", "finalize", "resume", "start", "end"}
IDLE_STATES = {"pause", "suspend", "tool"}
RELATIONS = {"fork", "spawn", "handoff"}
JOINS = {"sync", "async"}
CACHE_TYPES = {"ephemeral", "pin"}


class Issues:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, row: int, message: str) -> None:
        self.errors.append(f"row {row}: {message}")

    def warn(self, row: int, message: str) -> None:
        self.warnings.append(f"row {row}: {message}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_unknown(block: dict[str, Any], allowed: set[str], row: int, where: str, issues: Issues) -> None:
    for key in sorted(set(block) - allowed):
        issues.warn(row, f"{where} has unknown field {key!r}")


def _check_priority(value: Any, row: int, where: str, issues: Issues) -> None:
    if value is None:
        return
    if _is_int(value):
        issues.warn(row, f"{where} is an int; prefer {{'level': ..., 'reason': ...}}")
        if value < 0:
            issues.error(row, f"{where} must be >= 0")
        return
    if not isinstance(value, dict):
        issues.error(row, f"{where} must be a dict or int")
        return
    _check_unknown(value, {"level", "reason"}, row, where, issues)
    level = value.get("level")
    if not _is_int(level) or level < 0:
        issues.error(row, f"{where}.level must be an int >= 0")
    reason = value.get("reason")
    if reason is not None and not isinstance(reason, str):
        issues.error(row, f"{where}.reason must be a string")


def _check_cache_control(value: Any, row: int, issues: Issues) -> None:
    if value is None:
        return
    if isinstance(value, list):
        issues.error(row, "cache_control must be a single dict; list form is not supported")
        return
    if not isinstance(value, dict):
        issues.error(row, "cache_control must be a dict")
        return
    _check_unknown(value, {"type", "ttl", "ttl_s", "msg_offset", "range"}, row, "cache_control", issues)
    cache_type = value.get("type", "ephemeral")
    if cache_type not in CACHE_TYPES:
        issues.error(row, f"cache_control.type must be one of {sorted(CACHE_TYPES)}, got {cache_type!r}")
    for key in ("ttl", "ttl_s"):
        ttl = value.get(key)
        if ttl is None:
            continue
        if not _is_int(ttl):
            issues.error(row, f"cache_control.{key} must be an int")
        elif not 60 <= ttl <= 3600:
            issues.warn(row, f"cache_control.{key}={ttl} is outside [60, 3600]; Motor clamps it")
    msg_offset = value.get("msg_offset")
    if msg_offset is not None and (not _is_int(msg_offset) or msg_offset < 0):
        issues.error(row, "cache_control.msg_offset must be an int >= 0")
    range_data = value.get("range")
    if range_data is not None:
        if not isinstance(range_data, dict):
            issues.error(row, "cache_control.range must be a dict")
        else:
            _check_unknown(range_data, {"start", "end", "unit"}, row, "cache_control.range", issues)
            start = range_data.get("start")
            if start is not None and (not _is_int(start) or start < 0):
                issues.error(row, "cache_control.range.start must be an int >= 0")
            elif start not in (None, 0):
                issues.warn(row, "cache_control.range.start > 0 is not supported by Motor; range will be dropped")
            end = range_data.get("end")
            if end is not None and (not _is_int(end) or end < 0):
                issues.error(row, "cache_control.range.end must be an int >= 0")
            unit = range_data.get("unit", "message")
            if unit not in (None, "", "message"):
                issues.error(row, f"cache_control.range.unit only supports 'message', got {unit!r}")
            if msg_offset is not None and end is not None:
                issues.warn(row, "both cache_control.msg_offset and range.end are set; legacy msg_offset wins")


def _check_prediction(value: Any, row: int, issues: Issues) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.error(row, "prediction must be a dict")
        return
    for key in ("latency_ms", "remaining_calls", "output_tokens", "interval_ms"):
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
            issues.error(row, f"prediction.{key} must be a number >= 0")


def _check_hint(hint: Any, row: int, issues: Issues) -> dict[str, Any]:
    if not isinstance(hint, dict):
        issues.error(row, "agent_hint must be a dict")
        return {}
    _check_unknown(hint, ALLOWED_TOP_LEVEL, row, "agent_hint", issues)
    for key in sorted(set(hint) & BODY_ONLY_TOP_LEVEL):
        issues.warn(row, f"agent_hint.{key} is a legacy body-only field; v2 header consumers ignore it")
    if "stage" in hint:
        issues.warn(row, "agent_hint.stage is legacy; use agent_status.state")

    session = hint.get("session")
    if not isinstance(session, dict):
        issues.error(row, "agent_hint.session must be a dict")
        session = {}
    else:
        _check_unknown(
            session,
            {"session_id", "parent_session_id", "derivation", "agent_type", "call_index", "epoch"},
            row,
            "session",
            issues,
        )
        sid = session.get("session_id")
        if not isinstance(sid, str) or not sid:
            issues.error(row, "session.session_id must be a non-empty string")
        parent = session.get("parent_session_id")
        if parent is not None and (not isinstance(parent, str) or not parent):
            issues.error(row, "session.parent_session_id must be a non-empty string or null")
        derivation = session.get("derivation")
        if derivation is not None:
            if not isinstance(derivation, dict):
                issues.error(row, "session.derivation must be a dict")
            else:
                _check_unknown(derivation, {"relation", "join"}, row, "session.derivation", issues)
                relation = derivation.get("relation")
                if relation is not None and relation not in RELATIONS:
                    issues.error(row, f"session.derivation.relation must be one of {sorted(RELATIONS)}")
                join = derivation.get("join")
                if join is not None and join not in JOINS:
                    issues.error(row, f"session.derivation.join must be one of {sorted(JOINS)}")
        agent_type = session.get("agent_type")
        if agent_type is not None and not isinstance(agent_type, str):
            issues.error(row, "session.agent_type must be a string")
        for key in ("epoch", "call_index"):
            value = session.get(key)
            if value is not None and (not _is_int(value) or value < 0):
                issues.error(row, f"session.{key} must be an int >= 0")

    status = hint.get("agent_status")
    if status is None:
        issues.warn(row, "agent_hint.agent_status is missing; the request will not declare a lifecycle state")
    elif not isinstance(status, dict):
        issues.error(row, "agent_hint.agent_status must be a dict")
    else:
        _check_unknown(status, {"state", "tools"}, row, "agent_status", issues)
        state = status.get("state")
        if state not in ALLOWED_STATES:
            issues.error(row, f"agent_status.state must be one of {sorted(ALLOWED_STATES)}, got {state!r}")
        elif state in {"resume", "start", "end"}:
            issues.warn(row, f"agent_status.state={state!r} is an AISBench alias; it will be normalized on the wire")
        tools = status.get("tools")
        if tools is not None and not isinstance(tools, list):
            issues.error(row, "agent_status.tools must be a list")

    _check_priority(hint.get("priority"), row, "priority", issues)
    _check_priority(hint.get("strict_priority"), row, "strict_priority", issues)
    _check_cache_control(hint.get("cache_control"), row, issues)
    _check_prediction(hint.get("prediction"), row, issues)

    try:
        header = encode_agent_hint_header(hint)
    except Exception as exc:  # noqa: BLE001
        issues.error(row, f"agent_hint cannot be encoded: {exc}")
        return session
    if len(header) > MAX_HEADER_CHARS:
        issues.error(row, f"encoded header is {len(header)} chars, exceeding {MAX_HEADER_CHARS}")
    return session


def _parse_json_field(value: Any, name: str, row: int, issues: Issues) -> None:
    if value is None:
        return
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            issues.error(row, f"{name} is not valid JSON: {exc}")
            return
    if not isinstance(value, list):
        issues.error(row, f"{name} must be a list or a JSON list string")
        return
    if name == "tools" and not value:
        issues.warn(row, "tools is an empty list; vLLM rejects empty tools with 400, so do not send it")


def _check_row(
    row: dict[str, Any], row_no: int, issues: Issues
) -> tuple[str, int | None, str, dict[str, Any], Any]:
    hint = row.get("agent_hint")
    if hint is None:
        issues.error(row_no, "agent_hint is missing; this is not an Agent Hint dataset")
        return "", None, "", {}, None
    session = _check_hint(hint, row_no, issues)
    sid = session.get("session_id") if isinstance(session, dict) else ""
    epoch = session.get("epoch") if isinstance(session, dict) else None
    status = hint.get("agent_status") if isinstance(hint, dict) else None
    state = status.get("state") if isinstance(status, dict) else ""
    state = str(state).lower() if state is not None else ""

    if not row.get("question") and not row.get("messages"):
        issues.error(row_no, "row has neither question nor messages")
    max_out_len = row.get("max_out_len")
    if max_out_len is not None and (not _is_int(max_out_len) or max_out_len <= 0):
        issues.error(row_no, "max_out_len must be an int > 0")
    timestamp = row.get("timestamp")
    if timestamp is not None and (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or timestamp < 0
    ):
        issues.error(row_no, "timestamp must be a number >= 0")
    delay = row.get("delay_after_ms")
    if delay is not None and (not _is_int(delay) or delay < 0):
        issues.error(row_no, "delay_after_ms must be an int >= 0")
    resume_before = row.get("resume_before")
    if resume_before is not None and not isinstance(resume_before, bool):
        issues.error(row_no, "resume_before must be a boolean")
    _parse_json_field(row.get("messages"), "messages", row_no, issues)
    _parse_json_field(row.get("tools"), "tools", row_no, issues)
    top_sid = row.get("session_id")
    if top_sid is not None and sid and str(top_sid) != sid:
        issues.warn(row_no, f"top-level session_id {top_sid!r} differs from agent_hint session_id {sid!r}")
    if "stage" in row:
        issues.warn(row_no, "top-level stage is legacy; use agent_hint.agent_status.state")
    return str(sid), epoch, state, session, timestamp


def validate_dataset(path: Path, strict: bool) -> int:
    issues = Issues()
    records: list[tuple[int, dict[str, Any]]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for row_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    issues.error(row_no, f"invalid JSON: {exc}")
                    continue
                if not isinstance(row, dict):
                    issues.error(row_no, "row must be a JSON object")
                    continue
                records.append((row_no, row))
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    if not records:
        issues.error(0, "dataset is empty")

    parsed: list[tuple[int, str, int | None, str, dict[str, Any], Any]] = []
    for row_no, row in records:
        sid, epoch, state, session, timestamp = _check_row(row, row_no, issues)
        parsed.append((row_no, sid, epoch, state, session, timestamp))

    session_ids = {sid for _, sid, _, _, _, _ in parsed if sid}
    state_tracking: dict[str, dict[str, Any]] = {}
    for row_no, sid, epoch, state, session, timestamp in parsed:
        if not sid:
            continue
        track = state_tracking.setdefault(
            sid,
            {
                "last_epoch": None,
                "finalized_epoch": None,
                "seen_pause": False,
                "last_ts": None,
                "last_call_index": None,
                "last_state": None,
                "parent": None,
            },
        )
        finalized_epoch = track["finalized_epoch"]
        if finalized_epoch is not None:
            if epoch is None:
                issues.error(row_no, f"session {sid!r} has a row after finalize without an epoch")
            elif epoch <= finalized_epoch:
                issues.error(
                    row_no,
                    f"session {sid!r} has a row after finalize at epoch {epoch} "
                    f"(finalized at {finalized_epoch})",
                )
            else:
                issues.warn(row_no, f"session {sid!r} is revived by epoch {epoch} after finalize")
                track["finalized_epoch"] = None
        if epoch is not None and track["last_epoch"] is not None and epoch < track["last_epoch"]:
            issues.error(row_no, f"session {sid!r} epoch decreases from {track['last_epoch']} to {epoch}")
        if state == "finalize":
            if track["finalized_epoch"] is not None and (epoch or 0) <= track["finalized_epoch"]:
                issues.error(row_no, f"session {sid!r} finalize is duplicated without a newer epoch")
            track["finalized_epoch"] = epoch if epoch is not None else 0
        if state in IDLE_STATES:
            track["seen_pause"] = True
        if state in {"resume", "start"} and not track["seen_pause"]:
            issues.warn(row_no, f"session {sid!r} resumes without a preceding pause/suspend/tool")
        parent = session.get("parent_session_id")
        if parent:
            if track["parent"] is not None and parent != track["parent"]:
                issues.warn(row_no, f"session {sid!r} parent changes from {track['parent']!r} to {parent!r}")
            track["parent"] = parent
            if parent != sid and parent not in session_ids:
                issues.warn(row_no, f"session {sid!r} references parent {parent!r} that is not present in this dataset")
        if timestamp is not None:
            if track["last_ts"] is not None and timestamp < track["last_ts"]:
                issues.error(row_no, f"session {sid!r} timestamp decreases from {track['last_ts']} to {timestamp}")
            track["last_ts"] = timestamp
        call_index = session.get("call_index")
        if call_index is not None:
            if track["last_call_index"] is not None and call_index < track["last_call_index"]:
                issues.warn(
                    row_no,
                    f"session {sid!r} call_index decreases from "
                    f"{track['last_call_index']} to {call_index}",
                )
            track["last_call_index"] = call_index
        if epoch is not None:
            track["last_epoch"] = epoch
        track["last_state"] = state

    for sid, track in state_tracking.items():
        if track["last_state"] in IDLE_STATES:
            issues.warn(0, f"session {sid!r} ends in idle state {track['last_state']!r} without finalize")

    status = "FAIL" if issues.errors or (strict and issues.warnings) else "PASS"
    print(f"[{status}] {path}")
    print(
        f"  rows={len(records)} sessions={len(session_ids)} "
        f"errors={len(issues.errors)} warnings={len(issues.warnings)}"
    )
    for message in issues.errors:
        print(f"  ERROR {message}")
    for message in issues.warnings:
        print(f"  WARN  {message}")
    return 0 if status == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args()
    exit_code = 0
    for path in args.datasets:
        exit_code |= validate_dataset(path, args.strict)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
