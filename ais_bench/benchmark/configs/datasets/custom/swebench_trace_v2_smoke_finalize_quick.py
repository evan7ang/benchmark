from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

_DATA_DIR = __import__("os").environ.get("AGENT_HINT_LOCAL_DATA_DIR", "/home/t00887700/kv-hint/.build/aisbench/data")
TRACE_DATASET = _DATA_DIR + "/swebench_smoke_v2_finalize_quick.jsonl"

swebench_trace_v2_reader_cfg = dict(
    input_columns=["question", "max_out_len", "timestamp"],
    output_column=None,
)

swebench_trace_v2_infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{question}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

swebench_trace_v2_eval_cfg = dict(evaluator=None, pred_role="BOT")

swebench_trace_v2_datasets = [
    dict(
        abbr="swebench-trace-v2-quick",
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=swebench_trace_v2_reader_cfg,
        infer_cfg=swebench_trace_v2_infer_cfg,
        eval_cfg=swebench_trace_v2_eval_cfg,
    )
]
