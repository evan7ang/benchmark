from ais_bench.benchmark.models import VLLMCustomAPI

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-motor-agent-hint-v2-trace",
        path="/path/to/tokenizer",
        model="qwen",
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=2,
        agent_hint_transport="header",
        pass_agent_hint=True,
        agent_status_report=True,
        extra_headers={},
        json_extra_fields=["messages", "tools"],
        drop_none_extra_fields=[
            "tool_choice",
            "temperature",
            "top_p",
            "seed",
            "enable_thinking",
        ],
        use_timestamp=True,
        batch_size=2,
        request_rate=0,
        retry=2,
        api_key="",
        host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1"),
        host_port=int(__import__("os").environ.get("AISBENCH_HOST_PORT", "31036")),
        url="",
        max_out_len=4096,
        generation_kwargs=dict(
            temperature=0,
            ignore_eos=True,
        ),
    )
]
