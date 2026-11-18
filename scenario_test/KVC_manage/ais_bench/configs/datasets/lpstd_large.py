import os

from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

TRACE_DATASET = os.environ.get(
    "LPSTD_DATASET",
    "/home/t00887700/agent-hint/benchmark-ah/tests/manual/long_pause_std/data/lpstd_large_v16_a4x16.jsonl",
)

reader_cfg = dict(
    input_columns=["question", "max_out_len", "timestamp"],
    output_column=None,
)

infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{question}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

eval_cfg = dict(evaluator=None, pred_role="BOT")

lpstd_large_datasets = [
    dict(
        abbr="lpstd-large",
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=reader_cfg,
        infer_cfg=infer_cfg,
        eval_cfg=eval_cfg,
    )
]
