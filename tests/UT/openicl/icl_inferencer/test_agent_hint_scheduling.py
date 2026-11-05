import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from ais_bench.benchmark.openicl.icl_inferencer.icl_base_api_inferencer import (
    BaseApiInferencer,
)


def _v2(session_id, state, epoch=0):
    return {
        "agent_hint": {
            "session": {"session_id": session_id, "epoch": epoch},
            "agent_status": {"state": state},
        }
    }


class TestAgentHintScheduling(unittest.TestCase):
    def test_group_key_v2_uses_session_id(self):
        data = _v2("s-child", "running")
        data["agent_hint"]["session"]["parent_session_id"] = "s-parent"
        self.assertEqual(BaseApiInferencer._session_group_key(data), "s-child")

    def test_group_key_legacy_uses_parent_then_session(self):
        data = {
            "session_id": "s-child",
            "parent_session_id": "s-parent",
            "agent_hint": {"session_id": "s-child", "parent_session_id": "s-parent"},
        }
        self.assertEqual(BaseApiInferencer._session_group_key(data), "s-parent")
        data["parent_session_id"] = ""
        data["agent_hint"]["parent_session_id"] = ""
        self.assertEqual(BaseApiInferencer._session_group_key(data), "s-child")

    def test_is_end_legacy_and_v2(self):
        self.assertTrue(BaseApiInferencer._data_is_end({"stage": "end"}))
        self.assertTrue(BaseApiInferencer._data_is_end(_v2("s", "finalize")))
        self.assertFalse(BaseApiInferencer._data_is_end(_v2("s", "running")))

    def test_is_pause_and_resume(self):
        self.assertTrue(BaseApiInferencer._data_is_pause(_v2("s", "pause")))
        self.assertTrue(BaseApiInferencer._data_is_pause(_v2("s", "suspend")))
        self.assertFalse(BaseApiInferencer._data_is_pause(_v2("s", "running")))
        self.assertTrue(BaseApiInferencer._data_is_resume(_v2("s", "resume")))
        self.assertFalse(BaseApiInferencer._data_is_resume(_v2("s", "running")))

    def test_lifecycle_state_prefers_nested(self):
        data = {
            "stage": "end",
            "agent_hint": {
                "session": {"session_id": "s"},
                "agent_status": {"state": "pause"},
            },
        }
        self.assertEqual(
            BaseApiInferencer._data_lifecycle_state(data), "pause"
        )

    def test_report_agent_status_resume_maps_to_running(self):
        reporter = MagicMock()
        reporter.agent_hint_transport = "header"
        reporter.agent_status_report = True
        reporter.post_agent_status = AsyncMock(return_value=(200, "{}"))
        inf = MagicMock()
        inf.pass_agent_hint = True
        inf.model = reporter
        data = _v2("s-1", "resume", epoch=2)
        data["agent_hint"]["session"]["parent_session_id"] = "s-0"

        asyncio.run(
            BaseApiInferencer._report_agent_status(inf, MagicMock(), data, "resume")
        )

        payload = reporter.post_agent_status.call_args.args[0]
        self.assertEqual(payload["session"]["session_id"], "s-1")
        self.assertEqual(payload["session"]["epoch"], 2)
        self.assertEqual(payload["agent_status"]["state"], "running")

    def test_report_agent_status_disabled(self):
        reporter = MagicMock()
        reporter.agent_hint_transport = "header"
        reporter.agent_status_report = False
        reporter.post_agent_status = AsyncMock()
        inf = MagicMock()
        inf.pass_agent_hint = True
        inf.model = reporter

        asyncio.run(
            BaseApiInferencer._report_agent_status(
                inf, MagicMock(), _v2("s-1", "pause"), "pause"
            )
        )
        reporter.post_agent_status.assert_not_called()

    def test_report_agent_status_not_passed(self):
        reporter = MagicMock()
        reporter.agent_hint_transport = "header"
        reporter.agent_status_report = True
        reporter.post_agent_status = AsyncMock()
        inf = MagicMock()
        inf.pass_agent_hint = False
        inf.model = reporter

        asyncio.run(
            BaseApiInferencer._report_agent_status(
                inf, MagicMock(), _v2("s-1", "finalize"), "finalize"
            )
        )
        reporter.post_agent_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
