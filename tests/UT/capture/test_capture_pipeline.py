import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ais_bench.capture import model_io, recorder, v2_builder


def _hint(sid):
    return {
        "session": {"session_id": sid, "parent_session_id": sid, "agent_type": "agent"},
        "agent_status": {"state": "running"},
        "priority": {"level": 1, "reason": "critical_path"},
        "cache_control": [{"type": "ephemeral", "ttl_s": 60}],
    }


def _model_io_record(sid, ordinal, content="hi", completion=10, extra=None):
    request = {
        "messages": [{"role": "user", "content": content}],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
        "agent_hint": _hint(sid),
    }
    if extra:
        request.update(extra)
    return {
        "schema_version": "harness-capture-v1",
        "case_id": "case-1",
        "ordinal": ordinal,
        "global_request_ordinal": ordinal,
        "request_kind": "inference",
        "session_id": sid,
        "parent_session_id": sid,
        "timing": {"source_utc": {"start_time": 1000 + ordinal}},
        "usage": {"prompt_tokens": 100, "completion_tokens": completion},
        "request": request,
    }


class FakeTokenizer:
    def apply_chat_template(self, messages, tools=None, tokenize=False,
                            add_generation_prompt=True, enable_thinking=None, **kwargs):
        return str(messages[0]["content"])

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": list(range(int(text)))}


def _write_model_io(directory, records):
    for index, record in enumerate(records):
        with open(os.path.join(directory, f"capture_{index:06d}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(record, f)


def _build_args(input_dir, output, **overrides):
    values = dict(
        input_dir=input_dir,
        output=output,
        report=None,
        default_max_out_len=256,
        completion_token_cap=2048,
        state_mode="completed-finalize",
        status_events=None,
        tokenizer=None,
        max_context=None,
        skip_tokenizer_check=True,
        replay_pause_events=False,
        pause_delay_min_ms=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class TestRecorderUsage(unittest.TestCase):
    def test_feed_usage_handles_split_and_multiple_lines(self):
        usage, buf = recorder.feed_usage(b"", b'data: {"usage":{"completion_tokens":1}}\n')
        self.assertEqual(usage["completion_tokens"], 1)
        self.assertEqual(buf, b"")

        usage, buf = recorder.feed_usage(
            b"", b'data: {"usage":{"completion_tokens":2}}\n\ndata: [DONE]\n'
        )
        self.assertEqual(usage["completion_tokens"], 2)
        self.assertEqual(buf, b"")

        usage, buf = recorder.feed_usage(b'data: {"usa', b'ge":{"completion_tokens":3}}\n')
        self.assertEqual(usage["completion_tokens"], 3)


class TestModelIO(unittest.TestCase):
    def test_preserves_generation_fields_and_drops_max_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            requests = os.path.join(tmp, "requests.jsonl")
            out = os.path.join(tmp, "model_io")
            record = {
                "path": "/v1/chat/completions",
                "upstream_status": 200,
                "ts": 1,
                "body": {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "upstream",
                    "stream": True,
                    "max_tokens": 123,
                    "thinking": {"type": "disabled"},
                    "reasoning_effort": "high",
                    "tools": [{"type": "function", "function": {"name": "bash"}}],
                },
                "hint": _hint("s-1"),
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }
            with open(requests, "w", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            args = SimpleNamespace(
                input=requests, out=out, hints="on", case_id="case-1", session_id=None
            )
            model_io.convert(args)

            files = [p for p in os.listdir(out) if p.endswith(".json")
                     and p != "capture.report.json"]
            self.assertEqual(len(files), 1)
            saved = json.load(open(os.path.join(out, files[0]), encoding="utf-8"))
            request = saved["request"]
            self.assertEqual(request["thinking"], {"type": "disabled"})
            self.assertEqual(request["reasoning_effort"], "high")
            self.assertNotIn("max_tokens", request)
            self.assertIn("tools", request)


class TestV2Builder(unittest.TestCase):
    def test_status_events_drive_finalize_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = os.path.join(tmp, "model_io")
            os.makedirs(input_dir)
            _write_model_io(input_dir, [
                _model_io_record("s-1", 1),
                _model_io_record("s-2", 2),
            ])
            status = os.path.join(tmp, "status.jsonl")
            with open(status, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "session": {"session_id": "s-1"},
                    "agent_status": {"state": "finalize", "state_since_ms": 123},
                }) + "\n")
            output = os.path.join(tmp, "out.jsonl")
            args = _build_args(input_dir, output, status_events=status)
            v2_builder.build(args)

            rows = [json.loads(line) for line in open(output, encoding="utf-8")]
            by_sid = {row["agent_hint"]["session"]["session_id"]: row for row in rows}
            self.assertEqual(by_sid["s-1"]["agent_hint"]["agent_status"]["state"], "finalize")
            self.assertEqual(
                by_sid["s-1"]["agent_hint"]["agent_status"]["state_since_ms"], 123
            )
            self.assertNotEqual(
                by_sid["s-2"]["agent_hint"]["agent_status"]["state"], "finalize"
            )
            report = json.load(open(output + ".report.json", encoding="utf-8"))
            self.assertEqual(report["finalize_source"], "status_events")
            self.assertEqual(report["incomplete_sessions"], 1)

    def test_optional_columns_are_filled_on_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = os.path.join(tmp, "model_io")
            os.makedirs(input_dir)
            _write_model_io(input_dir, [
                _model_io_record("s-1", 1),
                _model_io_record("s-1", 2, extra={"thinking": {"type": "enabled"}}),
            ])
            output = os.path.join(tmp, "out.jsonl")
            v2_builder.build(_build_args(input_dir, output))

            rows = [json.loads(line) for line in open(output, encoding="utf-8")]
            for row in rows:
                self.assertIn("thinking", row)
            self.assertIsNone(rows[0]["thinking"])
            self.assertEqual(rows[1]["thinking"], {"type": "enabled"})

    def test_replay_pause_events_creates_pause_resume_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = os.path.join(tmp, "model_io")
            os.makedirs(input_dir)
            records = [
                _model_io_record("s-1", 1),
                _model_io_record("s-1", 2),
                _model_io_record("s-1", 3),
                _model_io_record("s-1", 4),
            ]
            for record, start in zip(records, (1000, 5000, 9000, 13000)):
                record["timing"]["source_utc"]["start_time"] = start
            _write_model_io(input_dir, records)
            status = os.path.join(tmp, "status.jsonl")
            events = [
                {"session": {"session_id": "s-1"}, "agent_status": {"state": "pause", "state_since_ms": 2000}},
                {"session": {"session_id": "s-1"}, "agent_status": {"state": "running", "state_since_ms": 2100}},
                {"session": {"session_id": "s-1"}, "agent_status": {"state": "pause", "state_since_ms": 6000}},
                {"session": {"session_id": "s-1"}, "agent_status": {"state": "running", "state_since_ms": 6100}},
                {"session": {"session_id": "s-1"}, "agent_status": {"state": "finalize", "state_since_ms": 14000}},
            ]
            with open(status, "w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            output = os.path.join(tmp, "out.jsonl")
            args = _build_args(
                input_dir, output, status_events=status,
                replay_pause_events=True, pause_delay_min_ms=0,
            )
            v2_builder.build(args)

            rows = [json.loads(line) for line in open(output, encoding="utf-8")]
            self.assertEqual(rows[0]["agent_hint"]["agent_status"]["state"], "pause")
            self.assertEqual(rows[0]["delay_after_ms"], 3000)
            self.assertEqual(rows[1]["agent_hint"]["agent_status"]["state"], "pause")
            self.assertTrue(rows[1]["resume_before"])
            self.assertEqual(rows[1]["delay_after_ms"], 3000)
            self.assertTrue(rows[2]["resume_before"])
            self.assertIsNone(rows[2]["delay_after_ms"])
            self.assertEqual(rows[3]["agent_hint"]["agent_status"]["state"], "finalize")
            report = json.load(open(output + ".report.json", encoding="utf-8"))
            self.assertEqual(report["pause_event_replay"]["applied"], 2)
            self.assertEqual(report["pause_event_replay"]["resume_before_rows"], 2)

    def test_tokenizer_context_trim_and_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = os.path.join(tmp, "model_io")
            os.makedirs(input_dir)
            _write_model_io(input_dir, [
                _model_io_record("s-1", 1, content="100", completion=100),
                _model_io_record("s-1", 2, content="250", completion=100),
                _model_io_record("s-1", 3, content="350", completion=100),
            ])
            output = os.path.join(tmp, "out.jsonl")
            args = _build_args(
                input_dir, output, tokenizer="/fake", max_context=300,
                skip_tokenizer_check=False,
            )
            with mock.patch.object(v2_builder, "_load_tokenizer",
                                   return_value=FakeTokenizer()):
                v2_builder.build(args)

            rows = [json.loads(line) for line in open(output, encoding="utf-8")]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["max_out_len"], 100)
            self.assertEqual(rows[1]["max_out_len"], 50)
            report = json.load(open(output + ".report.json", encoding="utf-8"))
            self.assertEqual(report["context_dropped"], 1)
            self.assertEqual(report["context_trimmed"], 1)


if __name__ == "__main__":
    unittest.main()
