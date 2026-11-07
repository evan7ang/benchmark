from ais_bench.benchmark.models import VLLMCustomAPI

_C = 8

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr=f"vllm-motor-dsh-capture-off-c{_C}",
        path="/home/RAG_data/models/Qwen3-8B",
        model="kvh",
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=_C,
        agent_hint_transport="header",
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
            "thinking",
            "reasoning_effort",
            "response_format",
            "parallel_tool_calls",
        ],
        use_timestamp=False,
        batch_size=_C,
        request_rate=0,
        retry=2,
        api_key="",
        host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1"),
        host_port=int(__import__("os").environ.get("AISBENCH_HOST_PORT", "19325")),
        url="",
        max_out_len=4096,
        generation_kwargs=dict(
            temperature=0,
            ignore_eos=True,
        ),
    )
]
