from ais_bench.benchmark.models import VLLMCustomAPI

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-motor-agent-hint-v2",
        path="/path/to/tokenizer",
        model="qwen",
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=4,
        agent_hint_transport="header",
        pass_agent_hint=True,
        agent_status_report=True,
        extra_headers={},
        batch_size=4,
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="",
        host_ip=__import__("os").environ.get("AISBENCH_HOST_IP", "127.0.0.1"),
        host_port=int(__import__("os").environ.get("AISBENCH_HOST_PORT", "31036")),
        max_out_len=256,
        generation_kwargs=dict(
            temperature=0,
            ignore_eos=False,
        ),
    )
]
