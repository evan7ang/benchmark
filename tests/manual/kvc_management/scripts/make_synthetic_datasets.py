#!/usr/bin/env python3
"""Generate deterministic synthetic KVC datasets with the model tokenizer.

Runs inside the AISBench image (transformers available). No SWE-bench trace or
other external data is required: prompts are built from a deterministic token
corpus and validated against MAX_MODEL_LEN.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

WORDS = (
    "the quick brown fox jumps over the lazy dog while the system records "
    "deterministic context for prefix cache validation and lifecycle testing "
    "the scheduler keeps the session stable across pause resume finalize events "
    "memory blocks are reused when the prompt prefix is identical"
).split()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def make_text(tokenizer, target_tokens: int, seed: int) -> str:
    """Return text whose tokenization is approximately target_tokens."""
    if target_tokens < 32:
        target_tokens = 32
    rng = random.Random(seed)
    marker = f"[session-context-{seed}] "
    # A repeated natural-language paragraph; the marker makes sessions unique.
    paragraph = marker + " ".join(WORDS) + ". "
    # Over-build, then slice to the target token count.
    repeat = max(8, target_tokens // 12 + 8)
    text = paragraph * repeat
    ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    if len(ids) > target_tokens:
        ids = ids[:target_tokens]
    return tokenizer.decode(ids, skip_special_tokens=True)


def render_chat(tokenizer, text: str):
    messages = [{"role": "user", "content": text}]
    result = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True
    )
    if hasattr(result, "keys") and "input_ids" in result:
        result = result["input_ids"]
    return messages, len(result)


def fit_prompt(tokenizer, target: int, seed: int, chat_api: bool, max_allowed: int):
    """Build a prompt near ``target`` tokens and fit it into ``max_allowed``."""
    target = min(target, max_allowed)
    for _ in range(6):
        text = make_text(tokenizer, target, seed)
        if chat_api:
            messages, tokens = render_chat(tokenizer, text)
        else:
            messages = None
            tokens = len(tokenizer(text, add_special_tokens=True)["input_ids"])
        if tokens <= max_allowed:
            return text, messages, tokens
        target -= (tokens - max_allowed) + 32
        if target < 256:
            target = 256
    raise RuntimeError(
        f"cannot fit prompt seed={seed}: rendered={tokens} max={max_allowed}"
    )


def scale_specs(scale: str) -> dict:
    if scale == "fast":
        idle = int(env("FAST_IDLE_OFFLOAD", "10"))
        delay = int(env("FAST_DELAY_MS", "15000"))
        return {
            "idle_ms": idle * 1000,
            "delay_ms": max(delay, idle * 1000 + 5000),
            "functional": dict(sessions=3, turns=5, pause_turns=[2], base=8192, step=1024, cap=12000, max_out=256),
            "finalize": dict(sessions=4, turns=4, pause_turns=[], base=8192, step=1024, cap=12000, max_out=256),
            "gate": dict(sessions=1, turns=5, pause_turns=[2], base=2048, step=512, cap=4096, max_out=64),
            "multi": dict(sessions=10, turns=5, pause_turns=[2], base=16384, step=2048, cap=30000, max_out=128),
            "saturation_pause": dict(sessions=10, turns=5, pause_turns=[0], base=16384, step=2048, cap=30000, max_out=128),
            "saturation": dict(sessions=10, turns=4, pause_turns=[], base=16384, step=2048, cap=30000, max_out=128),
        }
    idle = int(env("STD_IDLE_OFFLOAD", "30"))
    delay = int(env("STD_DELAY_MS", "45000"))
    return {
        "idle_ms": idle * 1000,
        "delay_ms": max(delay, idle * 1000 + 5000),
        "functional": dict(sessions=5, turns=5, pause_turns=[2], base=16384, step=2048, cap=30000, max_out=512),
        "finalize": dict(sessions=6, turns=4, pause_turns=[], base=16384, step=2048, cap=30000, max_out=512),
        "gate": dict(sessions=1, turns=5, pause_turns=[2], base=4096, step=1024, cap=8192, max_out=128),
        "multi": dict(sessions=10, turns=5, pause_turns=[2], base=24576, step=1024, cap=30000, max_out=128),
        "saturation_pause": dict(sessions=10, turns=5, pause_turns=[0], base=24576, step=1024, cap=30000, max_out=128),
        "saturation": dict(sessions=10, turns=4, pause_turns=[], base=24576, step=1024, cap=30000, max_out=128),
    }


def states_for_turn(index: int, turns: int, pause_turns: list[int]) -> str:
    if index in pause_turns:
        return "pause"
    if index - 1 in pause_turns:
        return "resume"
    if index == turns - 1:
        return "finalize"
    return "running"


def fit_existing(tokenizer, text: str, max_allowed: int, chat_api: bool):
    """Trim an existing cumulative prompt until it fits the context window."""
    for _ in range(8):
        if chat_api:
            messages, tokens = render_chat(tokenizer, text)
        else:
            messages = None
            tokens = len(tokenizer(text, add_special_tokens=True)["input_ids"])
        if tokens <= max_allowed:
            return text, messages, tokens
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        if len(ids) <= 64:
            break
        keep = max(64, len(ids) - (tokens - max_allowed) - 64)
        text = tokenizer.decode(ids[:keep], skip_special_tokens=True)
    raise RuntimeError(f"cannot fit cumulative prompt: rendered={tokens} max={max_allowed}")


def generate_dataset(
    tokenizer,
    *,
    name: str,
    spec: dict,
    scale: str,
    delay_ms: int,
    max_model_len: int,
    chat_api: bool,
    out_dir: Path,
    seed: int,
) -> dict:
    sessions = spec["sessions"]
    turns = spec["turns"]
    pause_turns = list(spec["pause_turns"])
    max_out = spec["max_out"]
    max_allowed = max_model_len - max_out - 256
    rows = []
    token_counts = []
    timestamp = 1790000000000

    for s in range(sessions):
        session_id = f"bench_synthetic_{scale}_{name}_{s:02d}"
        session_seed = seed + s * 100003
        prefix_text, _, _ = fit_prompt(
            tokenizer, min(spec["base"], max_allowed), session_seed, chat_api, max_allowed
        )
        blocks: list[str] = []
        for t in range(turns):
            if t > 0:
                block_text, _, _ = fit_prompt(
                    tokenizer,
                    min(spec["step"], max_allowed),
                    session_seed + t * 7919,
                    chat_api,
                    max_allowed,
                )
                blocks.append(block_text)
            prompt_text = prefix_text
            if blocks:
                prompt_text = prefix_text + "\n\n" + "\n\n".join(blocks)
            prompt_text, messages, tokens = fit_existing(
                tokenizer, prompt_text, max_allowed, chat_api
            )
            state = states_for_turn(t, turns, pause_turns)
            row = {
                "question": f"syn-{scale}-{name}-{s:02d}-t{t + 1}" if chat_api else prompt_text,
                "max_out_len": max_out,
                "timestamp": timestamp,
                "agent_hint": {
                    "session": {
                        "session_id": session_id,
                        "parent_session_id": session_id,
                    },
                    "agent_status": {"state": state},
                },
                "delay_after_ms": delay_ms if state == "pause" else None,
            }
            if chat_api:
                row["messages"] = json.dumps(messages, ensure_ascii=False)
                row["tools"] = "[]"
            rows.append(row)
            token_counts.append(tokens)
            timestamp += 1000

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    states = Counter((row["agent_hint"]["agent_status"]["state"]) for row in rows)
    report = {
        "name": name,
        "scale": scale,
        "rows": len(rows),
        "sessions": sessions,
        "states": dict(states),
        "prompt_tokens": {
            "min": min(token_counts) if token_counts else 0,
            "max": max(token_counts) if token_counts else 0,
            "median": sorted(token_counts)[len(token_counts) // 2] if token_counts else 0,
        },
        "max_out_len": max_out,
        "delay_after_ms": delay_ms,
        "chat_api": chat_api,
        "output": str(path),
    }
    (out_dir / f"{name}.jsonl.report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({"name": name, "rows": len(rows), "sessions": sessions, "prompt_max": report["prompt_tokens"]["max"]}))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["fast", "standard", "all"], default="all")
    parser.add_argument("--out", default=env("DATA_DIR", "."))
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()

    model_path = env("TOKENIZER_PATH") or env("MODEL_PATH")
    if not model_path:
        raise SystemExit("TOKENIZER_PATH/MODEL_PATH is not set")
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    chat_api = env("CHAT_API", "true").lower() in ("1", "true", "yes", "on")
    if chat_api and not getattr(tokenizer, "chat_template", None):
        raise SystemExit(
            "CHAT_API=true but the tokenizer has no chat_template; set CHAT_API=false"
        )
    max_model_len = int(env("MAX_MODEL_LEN", "32768"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    scales = ["fast", "standard"] if args.scale == "all" else [args.scale]
    for scale in scales:
        specs = scale_specs(scale)
        # The generated filenames match the AISBench dataset configs.
        mapping = {
            "functional": "kvc_manual_pause_offload_functional",
            "finalize": "kvc_manual_finalize_quick",
            "gate": "kvc_manual_gate_quick",
            "multi": "kvc_manual_pause_offload_quick",
            "saturation_pause": "kvc_manual_saturation_pause",
            "saturation": "kvc_manual_saturation",
        }
        for mode, name in mapping.items():
            actual_name = name + ("_fast" if scale == "fast" else "")
            generate_dataset(
                tokenizer,
                name=actual_name,
                spec=specs[mode],
                scale=scale,
                delay_ms=specs["delay_ms"],
                max_model_len=max_model_len,
                chat_api=chat_api,
                out_dir=out_dir,
                seed=args.seed + (1 if scale == "fast" else 2),
            )


if __name__ == "__main__":
    main()
