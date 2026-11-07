#!/usr/bin/env python3
"""Check AISBench Agent Hint send-side audit against the source JSONL dataset.

Run AISBench with ``AISBENCH_AGENT_HINT_AUDIT_PATH`` pointing at a JSONL file,
then compare every audited ``X-Agent-Hint`` payload and ``/agent-status`` event
with the dataset that was supposed to be replayed.

Usage:
    AISBENCH_AGENT_HINT_AUDIT_PATH=/tmp/agent_hint_audit.jsonl \
    ais_bench --models ... --datasets ...

    python3 ais_bench/datasets/agent/check_send_side.py \
        --dataset ais_bench/datasets/agent/example_v2.jsonl \
        --audit /tmp/agent_hint_audit.jsonl --expect on
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ais_bench.benchmark.utils.agent_hint import (  # noqa: E402
    encode_agent_hint_header,
    lifecycle_state_of,
    normalize_agent_hint_for_wire,
    session_id_of,
)

HEADER_LOG_RE = re.compile(
    r"\[agent_hint\]\[send-header\] data_id=(?P<data_id>\S+) "
    r"session_id=(?P<session_id>\S+) state=(?P<state>\S+) header_len=(?P<header_len>\d+)"
)
STATUS_LOG_RE = re.compile(
    r"\[agent_hint\]\[agent-status\] session_id=(?P<session_id>\S+) "
    r"state=(?P<state>\S+) status=(?P<status>\d+)"
)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _load_jsonl(path: Path, report: Report, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.error(f"{label}:{line_no}: invalid JSON: {exc}")
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    report.error(f"{label}:{line_no}: record is not a JSON object")
    except OSError as exc:
        report.error(f"cannot read {label} {path}: {exc}")
    return rows


def _as_data_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _diff_values(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if type(expected) is not type(actual):
        return [f"{path}: type {type(expected).__name__} != {type(actual).__name__}"]
    if isinstance(expected, dict):
        diffs: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                diffs.append(f"{path}.{key}: unexpected actual field")
            elif key not in actual:
                diffs.append(f"{path}.{key}: missing actual field")
            else:
                diffs.extend(_diff_values(expected[key], actual[key], f"{path}.{key}"))
        return diffs
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: list length {len(expected)} != {len(actual)}"]
        diffs = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            diffs.extend(_diff_values(left, right, f"{path}[{index}]"))
        return diffs
    if expected != actual:
        return [f"{path}: {expected!r} != {actual!r}"]
    return []


def _expectation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        hint = row.get("agent_hint")
        if not isinstance(hint, dict):
            continue
        expected.append(
            {
                "data_id": index,
                "session_id": session_id_of(hint),
                "state": lifecycle_state_of(hint),
                "hint": normalize_agent_hint_for_wire(hint),
                "header": encode_agent_hint_header(hint),
            }
        )
    return expected


def _expected_status_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        hint = row.get("agent_hint")
        if not isinstance(hint, dict):
            continue
        state = lifecycle_state_of(hint).lower()
        sid = session_id_of(hint)
        resume_before = bool(row.get("resume_before"))
        if state in {"resume", "start"} or resume_before:
            events.append({"data_id": index, "session_id": sid, "state": "running", "when": "before"})
        if state in {"pause", "suspend", "tool", "finalize", "end"}:
            wire_state = "finalize" if state == "end" else state
            events.append({"data_id": index, "session_id": sid, "state": wire_state, "when": "after"})
    return events


def _counter_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_received(
    received: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    expected_events: list[dict[str, Any]],
    expect: str,
    report: Report,
) -> None:
    received_requests = [record for record in received if record.get("kind") == "received_request"]
    received_headers = [record for record in received if record.get("kind") == "received_header"]
    received_statuses = [record for record in received if record.get("kind") == "received_status"]
    if len(received_requests) != len(expected):
        report.error(f"mock received {len(received_requests)} chat requests, expected {len(expected)}")
    for record in received_requests:
        if record.get("body_has_agent_hint"):
            report.error("mock received a body that still contains agent_hint")
    if expect == "off":
        if received_headers:
            report.error(f"mock received {len(received_headers)} X-Agent-Hint headers in OFF mode")
        if received_statuses:
            report.error(f"mock received {len(received_statuses)} /agent-status events in OFF mode")
        return

    if len(received_headers) != len(expected):
        report.error(f"mock received {len(received_headers)} headers, expected {len(expected)}")
    expected_counter: dict[str, int] = defaultdict(int)
    for item in expected:
        expected_counter[_counter_key(item["hint"])] += 1
    actual_counter: dict[str, int] = defaultdict(int)
    for record in received_headers:
        actual_counter[_counter_key(record.get("hint"))] += 1
        if record.get("body_has_agent_hint"):
            report.error("mock received a body that still contains agent_hint")
    for key, count in expected_counter.items():
        if actual_counter.get(key, 0) != count:
            report.error(f"mock header mismatch: expected {count}, got {actual_counter.get(key, 0)}, hint={key[:200]}")
    for key, count in actual_counter.items():
        if key not in expected_counter:
            report.error(f"mock received unexpected header x{count}: {key[:200]}")

    if len(received_statuses) != len(expected_events):
        report.error(f"mock received {len(received_statuses)} status events, expected {len(expected_events)}")
    expected_status_counter: dict[tuple[str, str], int] = defaultdict(int)
    for event in expected_events:
        expected_status_counter[(event["session_id"], event["state"])] += 1
    actual_status_counter: dict[tuple[str, str], int] = defaultdict(int)
    for record in received_statuses:
        actual_status_counter[(str(record.get("session_id")), str(record.get("state")))] += 1
    for key, count in expected_status_counter.items():
        if actual_status_counter.get(key, 0) != count:
            report.error(f"mock status mismatch for {key}: expected {count}, got {actual_status_counter.get(key, 0)}")
    for key, count in actual_status_counter.items():
        if key not in expected_status_counter:
            report.error(f"mock received unexpected status {key} x{count}")


def _response_applied(record: dict[str, Any]) -> tuple[bool | None, str]:
    response = record.get("response")
    if not isinstance(response, str) or not response.strip():
        return None, ""
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    return payload.get("applied"), str(payload.get("reason", ""))


def check(
    dataset: Path,
    audit: Path,
    expect: str,
    log_path: Path | None,
    received_path: Path | None,
    strict: bool,
) -> int:
    report = Report()
    rows = _load_jsonl(dataset, report, "dataset")
    expected = _expectation_rows(rows)
    expected_events = _expected_status_events(rows)

    records = _load_jsonl(audit, report, "audit") if audit.exists() else []
    headers = [record for record in records if record.get("kind") == "header"]
    statuses = [record for record in records if record.get("kind") == "status"]

    if expect == "off":
        if headers:
            report.error(f"OFF expected no X-Agent-Hint, but audit has {len(headers)} header records")
        if statuses:
            report.error(f"OFF expected no /agent-status, but audit has {len(statuses)} status records")
    else:
        if not audit.exists():
            report.error(f"ON audit file does not exist: {audit}")
        by_data_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        unassigned: list[dict[str, Any]] = []
        for record in headers:
            data_id = _as_data_id(record.get("data_id"))
            if data_id is None:
                unassigned.append(record)
            else:
                by_data_id[data_id].append(record)

        used: set[int] = set()
        header_by_data_id: dict[int, dict[str, Any]] = {}
        for item in expected:
            data_id = item["data_id"]
            matches = by_data_id.get(data_id)
            if not matches:
                report.error(f"data_id={data_id}: missing X-Agent-Hint audit record")
                continue
            if len(matches) > 1:
                report.warn(f"data_id={data_id}: {len(matches)} header records (retry?)")
            record = matches[0]
            used.add(id(record))
            header_by_data_id[data_id] = record
            if record.get("session_id") != item["session_id"]:
                report.error(
                    f"data_id={data_id}: session_id {record.get('session_id')!r} != {item['session_id']!r}"
                )
            if record.get("state") != item["state"]:
                report.error(f"data_id={data_id}: state {record.get('state')!r} != {item['state']!r}")
            if record.get("header") != item["header"]:
                report.error(f"data_id={data_id}: encoded header differs from dataset")
            for diff in _diff_values(item["hint"], record.get("hint"), path=f"data_id={data_id}.hint"):
                report.error(diff)

        for record in headers:
            if id(record) not in used:
                report.error(f"unexpected header audit record: data_id={record.get('data_id')!r}")
        for record in unassigned:
            report.warn(f"header audit record without data_id: session_id={record.get('session_id')!r}")

        statuses_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in statuses:
            statuses_by_session[str(record.get("session_id"))].append(record)
        for items in statuses_by_session.values():
            items.sort(key=lambda record: float(record.get("ts", 0)))

        expected_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in expected_events:
            expected_by_session[event["session_id"]].append(event)

        for session_id, expected_items in expected_by_session.items():
            actual_items = statuses_by_session.get(session_id, [])
            if len(expected_items) != len(actual_items):
                report.error(
                    f"session {session_id!r}: expected {len(expected_items)} status events, got {len(actual_items)}"
                )
            for index, event in enumerate(expected_items):
                if index >= len(actual_items):
                    report.error(
                        f"session {session_id!r}: missing status {event['state']!r} for data_id={event['data_id']}"
                    )
                    continue
                actual = actual_items[index]
                if actual.get("state") != event["state"]:
                    report.error(
                        f"session {session_id!r}: status #{index} {actual.get('state')!r} != {event['state']!r}"
                    )
                payload = actual.get("payload") or {}
                payload_session = payload.get("session") if isinstance(payload, dict) else None
                payload_status = payload.get("agent_status") if isinstance(payload, dict) else None
                if not isinstance(payload_session, dict) or payload_session.get("session_id") != session_id:
                    report.error(f"session {session_id!r}: status payload session_id mismatch")
                if not isinstance(payload_status, dict) or payload_status.get("state") != event["state"]:
                    report.error(f"session {session_id!r}: status payload state mismatch")
                http_status = actual.get("status")
                if not isinstance(http_status, int) or http_status >= 400:
                    report.error(f"session {session_id!r}: status HTTP {http_status!r}")
                applied, reason = _response_applied(actual)
                if applied is False:
                    report.error(f"session {session_id!r}: status not applied: {reason}")
                elif applied is None:
                    report.warn(f"session {session_id!r}: status response has no parseable applied flag")
                header_record = header_by_data_id.get(event["data_id"])
                if header_record is not None:
                    status_ts = float(actual.get("ts", 0))
                    header_ts = float(header_record.get("ts", 0))
                    if event["when"] == "before" and status_ts > header_ts + 0.05:
                        report.error(f"session {session_id!r}: resume status was sent after the request")
                    if event["when"] == "after" and status_ts < header_ts - 0.05:
                        report.error(f"session {session_id!r}: {event['state']} status was sent before the request")

        for session_id in set(statuses_by_session) - set(expected_by_session):
            report.error(f"session {session_id!r}: unexpected /agent-status records")

    if received_path is not None:
        received = _load_jsonl(received_path, report, "received") if received_path.exists() else []
        if not received_path.exists():
            report.error(f"mock received file does not exist: {received_path}")
        else:
            _check_received(received, expected, expected_events, expect, report)

    if log_path is not None and log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        log_headers = [HEADER_LOG_RE.search(line) for line in text.splitlines()]
        log_headers = [match for match in log_headers if match]
        log_statuses = [STATUS_LOG_RE.search(line) for line in text.splitlines()]
        log_statuses = [match for match in log_statuses if match]
        if expect == "on":
            if len(log_headers) != len(expected):
                report.error(f"log: expected {len(expected)} send-header lines, got {len(log_headers)}")
            if len(log_statuses) != len(expected_events):
                report.error(f"log: expected {len(expected_events)} agent-status lines, got {len(log_statuses)}")
        else:
            if log_headers:
                report.error(f"log: OFF should have no send-header lines, got {len(log_headers)}")
            if log_statuses:
                report.error(f"log: OFF should have no agent-status lines, got {len(log_statuses)}")

    status = "FAIL" if report.errors or (strict and report.warnings) else "PASS"
    print(f"[{status}] dataset={dataset} audit={audit} expect={expect}")
    print(
        f"  dataset_rows={len(rows)} expected_headers={len(expected)} "
        f"expected_status={len(expected_events)} audit_headers={len(headers)} "
        f"audit_status={len(statuses)} errors={len(report.errors)} warnings={len(report.warnings)}"
    )
    for message in report.errors:
        print(f"  ERROR {message}")
    for message in report.warnings:
        print(f"  WARN  {message}")
    return 0 if status == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--expect", choices=("on", "off"), default="on")
    parser.add_argument("--log", type=Path, default=None, help="optional AISBench task .out log")
    parser.add_argument("--received", type=Path, default=None, help="optional mock server received JSONL")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args()
    return check(args.dataset, args.audit, args.expect, args.log, args.received, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
