#!/usr/bin/env python3
"""Render AISBench model/dataset configs from the resolved kvc_test.env.

The generated configs contain only values from the configuration, never the
original developer-machine paths. They are written under GENERATED_CONFIG_DIR
and mounted into the AISBench container at the same absolute path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DATASETS = {
    "kvc_manual_pause_offload_functional": "kvc_manual_pause_offload_functional.jsonl",
    "kvc_manual_pause_offload_quick": "kvc_manual_pause_offload_functional.jsonl",
    "kvc_manual_pause_offload_fast": "kvc_manual_pause_offload_functional_fast.jsonl",
    "kvc_manual_finalize_quick": "kvc_manual_finalize_quick.jsonl",
    "kvc_manual_finalize_fast": "kvc_manual_finalize_quick_fast.jsonl",
    "kvc_manual_gate_quick": "kvc_manual_gate_quick.jsonl",
    "kvc_manual_gate_fast": "kvc_manual_gate_quick_fast.jsonl",
    "kvc_manual_pause_offload_multi": "kvc_manual_pause_offload_quick.jsonl",
    "kvc_manual_saturation_pause": "kvc_manual_saturation_pause.jsonl",
    "kvc_manual_saturation_pause_fast": "kvc_manual_saturation_pause_fast.jsonl",
    "kvc_manual_saturation": "kvc_manual_saturation.jsonl",
    "kvc_manual_pause_offload_full": "kvc_manual_pause_offload_quick.jsonl",
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def model_config(concurrency: int, chat_api: bool) -> str:
    use_chat = "True" if chat_api else "False"
    extra_fields = '["messages", "tools"]' if chat_api else "[]"
    return f'''from ais_bench.benchmark.models import VLLMCustomAPI

_C = {concurrency}

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="kvc-manual-c{concurrency}",
        path={env("TOKENIZER_PATH")!r},
        model={env("MODEL_NAME")!r},
        stream=True,
        use_chat_api={use_chat},
        session_aware=True,
        session_concurrency=_C,
        agent_hint_transport="header",
        pass_agent_hint=True,
        agent_status_report=True,
        extra_headers={{}},
        json_extra_fields={extra_fields},
        drop_none_extra_fields=[
            "tool_choice",
            "temperature",
            "top_p",
            "seed",
            "enable_thinking",
        ],
        use_timestamp=False,
        batch_size=_C,
        request_rate=0,
        retry=2,
        api_key="",
        host_ip={env("HOST_IP", "127.0.0.1")!r},
        host_port={int(env("MOTOR_INFER_PORT", "19125"))},
        url="",
        max_out_len=4096,
        generation_kwargs=dict(temperature=0, ignore_eos=True),
    )
]
'''


def dataset_config(name: str, filename: str) -> str:
    path = str(Path(env("DATA_DIR")) / filename)
    return f'''from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

TRACE_DATASET = {path!r}

reader_cfg = dict(
    input_columns=["question", "max_out_len", "timestamp"],
    output_column=None,
)
infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{{question}}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)
eval_cfg = dict(evaluator=None, pred_role="BOT")

{name}_datasets = [
    dict(
        abbr={name.replace("_", "-")!r},
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=reader_cfg,
        infer_cfg=infer_cfg,
        eval_cfg=eval_cfg,
    )
]
'''


def main() -> None:
    out = Path(env("GENERATED_CONFIG_DIR"))
    (out / "models").mkdir(parents=True, exist_ok=True)
    (out / "datasets").mkdir(parents=True, exist_ok=True)
    chat_api = env("CHAT_API", "true").lower() in ("1", "true", "yes", "on")

    for concurrency in (1, 8, 16):
        (out / "models" / f"kvc_manual_c{concurrency}.py").write_text(
            model_config(concurrency, chat_api), encoding="utf-8"
        )
    for name, filename in DATASETS.items():
        (out / "datasets" / f"{name}.py").write_text(
            dataset_config(name, filename), encoding="utf-8"
        )

    meta = {
        "model_path": env("MODEL_PATH"),
        "tokenizer_path": env("TOKENIZER_PATH"),
        "model_name": env("MODEL_NAME"),
        "max_model_len": int(env("MAX_MODEL_LEN", "32768")),
        "chat_api": chat_api,
        "host_ip": env("HOST_IP", "127.0.0.1"),
        "host_port": int(env("MOTOR_INFER_PORT", "19125")),
        "data_dir": env("DATA_DIR"),
        "data_mode": env("KVC_DATA_MODE", "synthetic"),
        "generated_config_dir": str(out),
    }
    (out / "runtime.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(str(out))


if __name__ == "__main__":
    main()
