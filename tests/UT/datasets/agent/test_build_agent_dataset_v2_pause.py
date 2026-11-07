import copy
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "ais_bench"
    / "datasets"
    / "agent"
    / "build_agent_dataset_v2_pause.py"
)
SPEC = importlib.util.spec_from_file_location("build_agent_dataset_v2_pause", MODULE_PATH)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _row(session, state="running", max_out=512, content="hello"):
    return {
        "question": "q",
        "max_out_len": max_out,
        "messages": [{"role": "user", "content": content}],
        "agent_hint": {
            "session": {"session_id": session, "parent_session_id": session},
            "agent_status": {"state": state},
        },
    }


def _args(**overrides):
    values = dict(
        pause_every=3,
        min_session_requests=3,
        idle_offload_s=30,
        delay_ms=40000,
        jitter_ms=0,
        seed=0,
        max_context=32768,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeTokenizer:
    def apply_chat_template(self, messages, tools=None, tokenize=False,
                            add_generation_prompt=True):
        return str(messages[0]["content"])

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": list(range(int(text)))}


class TestPauseDatasetBuilder(unittest.TestCase):
    def test_pause_is_placed_on_nth_turn_and_next_is_resume(self):
        rows = [_row("s-1") for _ in range(6)] + [_row("s-1", "finalize")]
        args = _args()

        stats = builder.insert_pause_states(rows, args)

        self.assertEqual(stats["pause_points"], 1)
        self.assertEqual(stats["resume_points"], 1)
        states = [row["agent_hint"]["agent_status"]["state"] for row in rows]
        self.assertEqual(states[2], "pause")
        self.assertEqual(states[3], "resume")
        self.assertEqual(states[-1], "finalize")
        self.assertEqual(rows[2]["delay_after_ms"], 40000)

    def test_short_session_cannot_host_pause_resume_finalize(self):
        rows = [_row("s-short") for _ in range(3)] + [_row("s-short", "finalize")]
        stats = builder.insert_pause_states(rows, _args())

        self.assertEqual(stats["pause_points"], 0)
        self.assertEqual(
            [r["agent_hint"]["agent_status"]["state"] for r in rows],
            ["running", "running", "running", "finalize"],
        )
        self.assertEqual(stats["pause_skip_reasons"]["next_is_finalize"], 1)

    def test_last_row_is_never_rewritten_to_resume(self):
        rows = [_row("s-unfinished") for _ in range(5)]
        stats = builder.insert_pause_states(rows, _args())

        self.assertEqual(stats["pause_points"], 1)
        states = [row["agent_hint"]["agent_status"]["state"] for row in rows]
        self.assertEqual(states, ["running", "running", "pause", "resume", "running"])

    def test_finalize_is_not_synthesized(self):
        rows = [_row("s-unfinished") for _ in range(4)]
        builder.insert_pause_states(rows, _args())

        states = [row["agent_hint"]["agent_status"]["state"] for row in rows]
        self.assertNotIn("finalize", states)

    def test_no_cache_control_is_added(self):
        rows = [_row("s-1") for _ in range(6)]
        builder.insert_pause_states(rows, _args())

        for row in rows:
            self.assertNotIn("cache_control", row["agent_hint"])

    def test_delay_is_only_non_null_on_pause_rows(self):
        rows = [_row("s-1") for _ in range(6)] + [_row("s-1", "finalize")]
        builder.insert_pause_states(rows, _args())

        for row in rows:
            state = row["agent_hint"]["agent_status"]["state"]
            if state == "pause":
                self.assertEqual(row["delay_after_ms"], 40000)
            else:
                self.assertIsNone(row["delay_after_ms"])

    def test_all_rows_keep_delay_column_for_arrow_schema(self):
        rows = [_row("s-1") for _ in range(6)] + [_row("s-1", "finalize")]
        builder.insert_pause_states(rows, _args())

        self.assertTrue(all("delay_after_ms" in row for row in rows))

    def test_low_delay_is_rejected(self):
        rows = [_row("s-1") for _ in range(6)]
        with self.assertRaises(ValueError):
            builder.insert_pause_states(rows, _args(delay_ms=1000))

    def test_validation_passes_for_generated_rows(self):
        rows = [_row("s-1") for _ in range(6)] + [_row("s-1", "finalize")]
        orig_rows = [copy.deepcopy(row) for row in rows]
        args = _args()
        builder.insert_pause_states(rows, args)
        prompt_tokens = {id(row): 0 for row in rows}
        checks, problems = builder.validate_output(
            rows, orig_rows, list(range(len(rows))), prompt_tokens, args
        )

        self.assertTrue(checks["all_passed"], problems)
        self.assertTrue(checks["pause_followed_by_resume"])
        self.assertTrue(checks["last_row_not_resume"])
        self.assertTrue(checks["finalize_only_last"])

    def test_context_drop_and_trim(self):
        rows = [
            _row("s-1", content="100", max_out=100),
            _row("s-1", content="350", max_out=100),
            _row("s-1", content="250", max_out=100),
        ]
        kept, kept_orig, tokens, info = builder.fit_context(
            rows, FakeTokenizer(), max_context=300
        )

        self.assertEqual(len(kept), 2)
        self.assertEqual(kept_orig, [0, 2])
        self.assertEqual(kept[0]["max_out_len"], 100)
        self.assertEqual(kept[1]["max_out_len"], 50)
        self.assertEqual(len(info["trimmed"]), 1)
        self.assertEqual(len(info["dropped"]), 1)
        self.assertEqual(info["dropped"][0]["reason"], "prompt_exceeds_context")
        self.assertEqual(tokens[id(kept[0])], 100)


if __name__ == "__main__":
    unittest.main()
