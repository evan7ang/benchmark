from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

TRACE_DATASET = "/home/t00887700/benchmark-data/agent_replay_v2/kvc_manual/kvc_manual_saturation_pause.jsonl"

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

kvc_manual_saturation_pause_datasets = [
    dict(
        abbr="kvc-manual-saturation-pause",
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=reader_cfg,
        infer_cfg=infer_cfg,
        eval_cfg=eval_cfg,
    )
]
