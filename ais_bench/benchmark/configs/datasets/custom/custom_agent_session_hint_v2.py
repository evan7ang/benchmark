from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

agent_session_hint_v2_reader_cfg = dict(
    input_columns=["question", "max_out_len"],
    output_column=None,
)

agent_session_hint_v2_infer_cfg = dict(
    prompt_template=dict(
        type=PromptTemplate,
        template="{question}",
    ),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)

agent_session_hint_v2_eval_cfg = dict(
    evaluator=None,
    pred_role="BOT",
)

agent_session_hint_v2_datasets = [
    dict(
        abbr="agent-session-hint-v2",
        type=CustomDataset,
        path="ais_bench/datasets/agent/example_v2.jsonl",
        reader_cfg=agent_session_hint_v2_reader_cfg,
        infer_cfg=agent_session_hint_v2_infer_cfg,
        eval_cfg=agent_session_hint_v2_eval_cfg,
    )
]
