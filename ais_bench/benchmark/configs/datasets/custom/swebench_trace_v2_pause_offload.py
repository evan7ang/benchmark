from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

TRACE_DATASET = "/home/t00887700/benchmark-data/agent_replay_v2/swebench_full50_v2_pause_offload.jsonl"

swebench_trace_v2_pause_offload_reader_cfg = dict(
    input_columns=["question", "max_out_len", "timestamp"],
    output_column=None,
)

swebench_trace_v2_pause_offload_infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{question}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

swebench_trace_v2_pause_offload_eval_cfg = dict(evaluator=None, pred_role="BOT")

swebench_trace_v2_pause_offload_datasets = [
    dict(
        abbr="swebench-trace-v2-pause-offload",
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=swebench_trace_v2_pause_offload_reader_cfg,
        infer_cfg=swebench_trace_v2_pause_offload_infer_cfg,
        eval_cfg=swebench_trace_v2_pause_offload_eval_cfg,
    )
]
