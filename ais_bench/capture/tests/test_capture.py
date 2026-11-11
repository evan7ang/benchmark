import json
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from ais_bench.capture.batch import (
    _load_existing_run, _merge_model_io, _safe_extract_zip, prepare_workspace, read_tasks,
)
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
    assert hint["cache_control"]["type"] == "ephemeral"
    assert hint["prediction"]["remaining_calls"] == 2
    assert hint["agent_status"]["state"] == "running"
    report = json.loads((dataset.parent / (dataset.name + ".report.json")).read_text(encoding="utf-8"))
    assert report["cache_control_original_list_rows"] == 1
    assert report["cache_control_normalized"] == 1
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


def test_read_tasks_derives_prompt_from_problem_statement(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps({
        "instance_id": "repo__task-1",
        "problem_statement": "fix the bug",
        "turns": 2,
    }) + "\n", encoding="utf-8")
    args = Namespace(case_id_field="instance_id", prompt_field="problem_statement",
                     prompt_prefix=None, no_prompt_prefix=False)
    items = read_tasks(str(tasks), args)
    assert items[0]["task_id"] == "repo__task-1"
    assert items[0]["prompt"].endswith("fix the bug")
    assert "current repository" in items[0]["prompt"]


def test_prepare_workspace_extracts_zip_and_creates_git(tmp_path):
    workspace_zip = tmp_path / "repo.zip"
    with zipfile.ZipFile(workspace_zip, "w") as zf:
        zf.writestr("repo/README.md", "hello\n")
    args = Namespace(out=str(tmp_path / "run"), workspaces_dir=None,
                     workspace_field="workspace_zip", init_git=True)
    info = prepare_workspace({"task_id": "t1", "workspace_zip": str(workspace_zip)}, args)
    assert info["workspace"].endswith("/repo")
    assert (tmp_path / "run/workspaces/t1/repo/README.md").exists()
    assert info["git_commit"]
    assert (tmp_path / "run/workspaces/t1/repo/.git").is_dir()


def test_merge_model_io_removes_stale_files(tmp_path):
    src = tmp_path / "run" / "model_io"
    src.mkdir(parents=True)
    (src / "capture_000001.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "merged"
    dest.mkdir()
    (dest / "stale.json").write_text("{}", encoding="utf-8")

    _merge_model_io([{"task_id": "t1", "model_io": str(src)}], str(dest))

    assert (dest / "t1__capture_000001.json").exists()
    assert not (dest / "stale.json").exists()


def test_load_existing_run_requires_recorded_returncode(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "model_io").mkdir(parents=True)
    (run_dir / "requests.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "model_io" / "capture_000001.json").write_text("{}", encoding="utf-8")

    assert _load_existing_run({"task_id": "t1"}, str(run_dir)) is None

    (run_dir / "task.json").write_text(json.dumps({"returncode": 3}), encoding="utf-8")
    result = _load_existing_run({"task_id": "t1"}, str(run_dir))
    assert result["returncode"] == 3


def test_prepare_workspace_reextracts_when_zip_changes(tmp_path):
    zip1 = tmp_path / "one.zip"
    with zipfile.ZipFile(zip1, "w") as zf:
        zf.writestr("repo/old.txt", "old")
    zip2 = tmp_path / "two.zip"
    with zipfile.ZipFile(zip2, "w") as zf:
        zf.writestr("repo/new.txt", "new")
    args = Namespace(out=str(tmp_path / "run"), workspaces_dir=None,
                     workspace_field="workspace_zip", init_git=False)

    first = prepare_workspace({"task_id": "t1", "workspace_zip": str(zip1)}, args)
    assert (Path(first["workspace"]) / "old.txt").exists()

    second = prepare_workspace({"task_id": "t1", "workspace_zip": str(zip2)}, args)
    assert (Path(second["workspace"]) / "new.txt").exists()
    assert not (Path(second["workspace"]) / "old.txt").exists()


def test_safe_extract_rejects_parent_traversal(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "evil")
    with pytest.raises(ValueError):
        _safe_extract_zip(str(bad), str(tmp_path / "out"))
