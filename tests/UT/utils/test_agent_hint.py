import base64
import json
import unittest

from ais_bench.benchmark.utils.agent_hint import (
    agent_hint_perf_metadata,
    cache_control_of,
    call_index_of,
    encode_agent_hint_header,
    epoch_of,
    is_v2_agent_hint,
    lifecycle_state_of,
    normalize_agent_hint_for_wire,
    parent_session_id_of,
    priority_level_of,
    session_id_of,
)


class TestAgentHintUtils(unittest.TestCase):
    def test_nested_v2_fields(self):
        hint = {
            "session": {
                "session_id": "s-1",
                "parent_session_id": "s-0",
                "call_index": 2,
                "epoch": 3,
            },
            "agent_status": {"state": "pause"},
            "priority": {"level": 4},
            "strict_priority": {"level": 5},
            "cache_control": {"type": "ephemeral", "ttl_s": 600},
        }
        self.assertTrue(is_v2_agent_hint(hint))
        self.assertEqual(session_id_of(hint), "s-1")
        self.assertEqual(parent_session_id_of(hint), "s-0")
        self.assertEqual(lifecycle_state_of(hint), "pause")
        self.assertEqual(call_index_of(hint), 2)
        self.assertEqual(epoch_of(hint), 3)
        self.assertEqual(priority_level_of(hint), 4)
        self.assertEqual(priority_level_of(hint, strict=True), 5)
        self.assertEqual(cache_control_of(hint)["ttl_s"], 600)

    def test_legacy_flat_fields(self):
        hint = {
            "session_id": "s-legacy",
            "parent_session_id": "s-root",
            "stage": "end",
        }
        self.assertFalse(is_v2_agent_hint(hint))
        self.assertEqual(session_id_of(hint), "s-legacy")
        self.assertEqual(parent_session_id_of(hint), "s-root")
        self.assertEqual(lifecycle_state_of(hint), "end")

    def test_missing_hint(self):
        self.assertFalse(is_v2_agent_hint(None))
        self.assertEqual(session_id_of(None), "")
        self.assertEqual(parent_session_id_of(None), "")
        self.assertEqual(lifecycle_state_of(None), "")
        self.assertIsNone(epoch_of(None))
        self.assertIsNone(call_index_of(None))
        self.assertIsNone(priority_level_of(None))

    def test_normalize_resume_and_end(self):
        hint = {
            "session": {"session_id": "s-1"},
            "agent_status": {"state": "resume"},
        }
        normalized = normalize_agent_hint_for_wire(hint)
        self.assertEqual(normalized["agent_status"]["state"], "running")
        self.assertEqual(hint["agent_status"]["state"], "resume")

        hint["agent_status"]["state"] = "end"
        normalized = normalize_agent_hint_for_wire(hint)
        self.assertEqual(normalized["agent_status"]["state"], "finalize")

        hint["agent_status"]["state"] = "start"
        normalized = normalize_agent_hint_for_wire(hint)
        self.assertEqual(normalized["agent_status"]["state"], "running")

    def test_normalize_prunes_none_values(self):
        hint = {
            "session": {"session_id": "s-1", "parent_session_id": None},
            "agent_status": {"state": "running"},
            "cache_control": {"type": None, "ttl_s": None},
        }
        normalized = normalize_agent_hint_for_wire(hint)
        self.assertNotIn("parent_session_id", normalized["session"])
        self.assertNotIn("cache_control", normalized)

    def test_encode_header_roundtrip(self):
        hint = {
            "session": {"session_id": "s-1", "epoch": 1},
            "agent_status": {"state": "resume"},
            "priority": {"level": 3},
        }
        encoded = encode_agent_hint_header(hint)
        self.assertNotIn("=", encoded)
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = json.loads(
            base64.urlsafe_b64decode(padded).decode("utf-8")
        )
        self.assertEqual(decoded["session"]["session_id"], "s-1")
        self.assertEqual(decoded["agent_status"]["state"], "running")
        self.assertEqual(decoded["priority"]["level"], 3)

    def test_encode_header_unicode(self):
        encoded = encode_agent_hint_header(
            {"session": {"session_id": "会话-1"}, "agent_status": {"state": "running"}}
        )
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        self.assertEqual(decoded["session"]["session_id"], "会话-1")

    def test_perf_metadata(self):
        hint = {
            "session": {
                "session_id": "s-1",
                "parent_session_id": "s-0",
                "call_index": 2,
                "epoch": 1,
            },
            "agent_status": {"state": "finalize"},
            "priority": {"level": 3},
            "cache_control": {"type": "ephemeral", "ttl_s": 600},
        }
        metadata = agent_hint_perf_metadata(hint)
        self.assertEqual(metadata["parent_session_id"], "s-0")
        self.assertEqual(metadata["lifecycle_state"], "finalize")
        self.assertEqual(metadata["call_index"], 2)
        self.assertEqual(metadata["epoch"], 1)
        self.assertEqual(metadata["priority_level"], 3)
        self.assertEqual(metadata["cache_control_type"], "ephemeral")
        self.assertEqual(metadata["cache_control_ttl_s"], 600)


if __name__ == "__main__":
    unittest.main()
