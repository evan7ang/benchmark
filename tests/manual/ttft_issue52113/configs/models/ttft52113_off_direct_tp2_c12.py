from ais_bench.benchmark.models import VLLMCustomAPI

_C = 12

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="ttft52113-off-direct-tp2-c12",
        path="/home/RAG_data/models/Qwen3-8B",
        model="kvh",
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=_C,
        agent_hint_transport="body",
        pass_agent_hint=False,
        agent_status_report=False,
        extra_headers={},
        json_extra_fields=["messages", "tools"],
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
        host_ip="127.0.0.1",
        host_port=18200,
        url="",
        max_out_len=512,
        generation_kwargs=dict(temperature=0, ignore_eos=True),
    )
]
