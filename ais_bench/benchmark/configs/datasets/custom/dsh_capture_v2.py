from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

# Point this at the dataset produced by:
#   python -m ais_bench.capture build \
#     --input-dir <capture>/model_io --output <run>/dataset_v2.jsonl \
#     --builder capture --status-events <capture>/status.jsonl ...
# A `current` symlink is convenient for repeated A/B runs.
TRACE_DATASET = "/home/t00887700/benchmark-data/agent_capture_dsh/current/dataset_v2.jsonl"

dsh_capture_v2_reader_cfg = dict(
    input_columns=["question", "max_out_len", "timestamp"],
    output_column=None,
)

dsh_capture_v2_infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template="{question}"),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

dsh_capture_v2_eval_cfg = dict(evaluator=None, pred_role="BOT")

dsh_capture_v2_datasets = [
    dict(
        abbr="dsh-capture-v2",
        type=CustomDataset,
        path=TRACE_DATASET,
        reader_cfg=dsh_capture_v2_reader_cfg,
        infer_cfg=dsh_capture_v2_infer_cfg,
        eval_cfg=dsh_capture_v2_eval_cfg,
    )
]
