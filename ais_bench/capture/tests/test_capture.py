import json
from argparse import Namespace

from ais_bench.capture.model_io import convert
from ais_bench.capture.v2_builder import build
from ais_bench.capture.validate import validate

HINT = {
    "session": {"session_id": "s1", "parent_session_id": "s1", "agent_type": "agent", "call_index": 1},
    "priority": {"level": 3, "reason": "user_waiting"},
    "cache_control": [{"type": "ephemeral", "ttl_s": 60, "range": {"end": 2, "unit": "message"}}],
    "prediction": {"remaining_calls": 2, "output_tokens": 64},
    "agent_status": {"state": "running"},
}


def _capture(path):
    main = {
        "ts": 1.0, "path": "/v1/chat/completions",
        "headers": {"x-deepseek-harness-session-id": "s1"},
        "body": {"model": "kvh", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        "hint": HINT, "upstream_status": 200,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    aux = {
        "ts": 2.0, "path": "/v1/chat/completions",
        "headers": {"x-deepseek-harness-session-id": "s1"},
        "body": {"model": "kvh", "messages": [{"role": "user", "content": "title"}], "stream": True},
        "hint": None, "upstream_status": 200, "usage": {},
    }
    path.write_text(json.dumps(main) + "\n" + json.dumps(aux) + "\n", encoding="utf-8")


def _convert(tmp_path, hints, status_events=None):
    src = tmp_path / "capture.jsonl"
    _capture(src)
    out = tmp_path / f"mi_{hints}"
    convert(Namespace(input=str(src), out=str(out), hints=hints, case_id="t1",
                      session_id=None, status_events=status_events))
    return out


def _files(out):
    return [p for p in out.glob("*.json") if p.name not in ("capture.report.json", "status_events.jsonl")]


def test_hints_on_preserves_full_hint(tmp_path):
    out = _convert(tmp_path, "on")
    assert len(_files(out)) == 1  # auxiliary no-hint call dropped
    dataset = tmp_path / "ds_on.jsonl"
    build(Namespace(input_dir=str(out), output=str(dataset), report=None,
                    default_max_out_len=256, completion_token_cap=256, state_mode="running"))
    row = json.loads(dataset.read_text(encoding="utf-8").splitlines()[0])
    hint = row["agent_hint"]
    assert hint["session"]["session_id"] == "s1"
    assert hint["priority"]["level"] == 3
    assert hint["cache_control"][0]["type"] == "ephemeral"
    assert hint["prediction"]["remaining_calls"] == 2
    assert hint["agent_status"]["state"] == "running"
    assert validate(str(dataset), "on") == 0


def test_hints_off_keeps_all_requests_without_hints(tmp_path):
    out = _convert(tmp_path, "off")
    assert len(_files(out)) == 2  # auxiliary call kept, grouped by the harness session header
    dataset = tmp_path / "ds_off.jsonl"
    build(Namespace(input_dir=str(out), output=str(dataset), report=None,
                    default_max_out_len=256, completion_token_cap=256, state_mode="running"))
    rows = [json.loads(l) for l in dataset.read_text(encoding="utf-8").splitlines()]
    assert all(r["agent_hint"]["session"]["session_id"] == "s1" for r in rows)
    assert all("priority" not in r["agent_hint"] for r in rows)
    assert validate(str(dataset), "off") == 0


def test_status_sink_finalize_applied_to_last_request(tmp_path):
    sink = tmp_path / "status.jsonl"
    sink.write_text(json.dumps({
        "session": {"session_id": "s1"},
        "agent_status": {"state": "finalize", "state_since_ms": 123},
    }) + "\n", encoding="utf-8")
    out = _convert(tmp_path, "on", status_events=str(sink))
    dataset = tmp_path / "ds_status.jsonl"
    build(Namespace(input_dir=str(out), output=str(dataset), report=None,
                    default_max_out_len=256, completion_token_cap=256, state_mode="running"))
    row = json.loads(dataset.read_text(encoding="utf-8").splitlines()[-1])
    assert row["agent_hint"]["agent_status"]["state"] == "finalize"
    assert validate(str(dataset), "on", True) == 0
