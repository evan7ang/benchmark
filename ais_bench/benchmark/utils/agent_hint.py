"""Helpers for the Agent Hint protocol (legacy body and v2 header)."""

import base64
import copy
import json
from typing import Any, Dict, Optional

HEADER_NAME = "X-Agent-Hint"
MAX_HEADER_CHARS = 16384

_WIRE_STATE_ALIASES = {
    "resume": "running",
    "start": "running",
    "end": "finalize",
}


def _session_block(hint: Any) -> Dict[str, Any]:
    if not isinstance(hint, dict):
        return {}
    session = hint.get("session")
    return session if isinstance(session, dict) else {}


def is_v2_agent_hint(hint: Any) -> bool:
    if not isinstance(hint, dict):
        return False
    return isinstance(hint.get("session"), dict) or isinstance(
        hint.get("agent_status"), dict
    )


def session_id_of(hint: Any) -> str:
    session = _session_block(hint)
    value = session.get("session_id")
    if value is None and isinstance(hint, dict):
        value = hint.get("session_id")
    return str(value) if value is not None else ""


def parent_session_id_of(hint: Any) -> str:
    session = _session_block(hint)
    value = session.get("parent_session_id")
    if value is None and isinstance(hint, dict):
        value = hint.get("parent_session_id")
    return str(value) if value is not None else ""


def lifecycle_state_of(hint: Any) -> str:
    if not isinstance(hint, dict):
        return ""
    status = hint.get("agent_status")
    if isinstance(status, dict):
        state = status.get("state")
        if state is not None:
            return str(state)
    stage = hint.get("stage")
    return str(stage) if stage is not None else ""


def _int_value(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def epoch_of(hint: Any) -> Optional[int]:
    return _int_value(_session_block(hint).get("epoch"))


def call_index_of(hint: Any) -> Optional[int]:
    return _int_value(_session_block(hint).get("call_index"))


def priority_level_of(hint: Any, strict: bool = False) -> Optional[int]:
    if not isinstance(hint, dict):
        return None
    key = "strict_priority" if strict else "priority"
    value = hint.get(key)
    if isinstance(value, dict):
        return _int_value(value.get("level"))
    return _int_value(value)


def cache_control_of(hint: Any) -> Dict[str, Any]:
    if not isinstance(hint, dict):
        return {}
    value = hint.get("cache_control")
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else {}


def _prune_none(value: Any) -> Any:
    if isinstance(value, dict):
        result = {
            key: _prune_none(item)
            for key, item in value.items()
            if item is not None
        }
        return {
            key: item
            for key, item in result.items()
            if not (isinstance(item, dict) and not item)
        }
    if isinstance(value, list):
        return [_prune_none(item) for item in value]
    return value


def normalize_agent_hint_for_wire(hint: Any) -> Any:
    if not isinstance(hint, dict):
        return hint
    result = copy.deepcopy(hint)
    status = result.get("agent_status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, str):
            status["state"] = _WIRE_STATE_ALIASES.get(state.lower(), state)
    return _prune_none(result)


def encode_agent_hint_header(hint: Any) -> str:
    payload = normalize_agent_hint_for_wire(hint)
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def agent_hint_perf_metadata(hint: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    parent_session_id = parent_session_id_of(hint)
    if parent_session_id:
        metadata["parent_session_id"] = parent_session_id
    lifecycle_state = lifecycle_state_of(hint)
    if lifecycle_state:
        metadata["lifecycle_state"] = lifecycle_state
    call_index = call_index_of(hint)
    if call_index is not None:
        metadata["call_index"] = call_index
    epoch = epoch_of(hint)
    if epoch is not None:
        metadata["epoch"] = epoch
    priority_level = priority_level_of(hint)
    if priority_level is not None:
        metadata["priority_level"] = priority_level
    strict_priority_level = priority_level_of(hint, strict=True)
    if strict_priority_level is not None:
        metadata["strict_priority_level"] = strict_priority_level
    cache_control = cache_control_of(hint)
    cache_type = cache_control.get("type")
    if cache_type is not None:
        metadata["cache_control_type"] = str(cache_type)
    ttl = cache_control.get("ttl_s", cache_control.get("ttl"))
    if ttl is not None:
        ttl_value = _int_value(ttl)
        if ttl_value is not None:
            metadata["cache_control_ttl_s"] = ttl_value
    return metadata
