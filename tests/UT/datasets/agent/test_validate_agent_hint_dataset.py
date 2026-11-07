import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "ais_bench"
    / "datasets"
    / "agent"
    / "validate_agent_hint_dataset.py"
)
SPEC = importlib.util.spec_from_file_location("validate_agent_hint_dataset", MODULE_PATH)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def _row(state="running", cache_control=None, session="s-1"):
    hint = {
        "session": {"session_id": session},
        "agent_status": {"state": state},
    }
    if cache_control is not None:
        hint["cache_control"] = cache_control
    return {
        "question": "q",
        "max_out_len": 16,
        "agent_hint": hint,
    }


def _validate(rows, strict=False):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "data.jsonl"
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            return validator.validate_dataset(path, strict)


class TestAgentHintDatasetValidator(unittest.TestCase):
    def test_valid_dict_cache_control_passes(self):
        rows = [_row(cache_control={"type": "ephemeral", "ttl_s": 60, "range": {"end": 2}})]
        self.assertEqual(_validate(rows), 0)

    def test_list_cache_control_is_rejected(self):
        rows = [_row(cache_control=[{"type": "ephemeral", "ttl_s": 60}])]
        self.assertEqual(_validate(rows), 1)

    def test_duplicate_finalize_without_new_epoch_is_rejected(self):
        rows = [
            _row(state="finalize"),
            _row(state="finalize"),
        ]
        self.assertEqual(_validate(rows), 1)

    def test_resume_alias_is_only_a_warning(self):
        rows = [
            _row(state="pause"),
            _row(state="resume"),
            _row(state="finalize"),
        ]
        self.assertEqual(_validate(rows, strict=False), 0)
        self.assertEqual(_validate(rows, strict=True), 1)

    def test_resume_before_must_be_boolean(self):
        row = _row(state="pause")
        row["resume_before"] = "yes"
        self.assertEqual(_validate([row]), 1)


if __name__ == "__main__":
    unittest.main()
