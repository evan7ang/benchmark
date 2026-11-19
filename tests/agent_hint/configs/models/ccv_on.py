import os

from ais_bench.benchmark.models import VLLMCustomAPI

# Concurrency of the perf run: keep it >= the number of live sessions so the
# replay never serialises the trace.
_C = int(os.environ.get("CCV_CONCURRENCY", "48"))
_HOST = os.environ.get("CCV_HOST_IP", "127.0.0.1")
_PORT = int(os.environ.get("CCV_HOST_PORT", "8000"))
_MODEL_PATH = os.environ.get("CCV_MODEL_PATH", "/home/RAG_data/models/Qwen3-8B")
_MODEL_NAME = os.environ.get("CCV_SERVED_MODEL", "kvh")

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="ccv-on",
        path=_MODEL_PATH,
        model=_MODEL_NAME,
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=_C,
        # agent hint ON: the envelope is sent in the request body, and the
        # engine reports back per-session status.  This is the config used by
        # both the `cc` arm (dataset carries cache_control) and the `nocc` arm
        # (same dataset content without cache_control).
        agent_hint_transport="body",
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
        use_timestamp=False,
        batch_size=_C,
        request_rate=0,
        retry=2,
        api_key="",
        host_ip=_HOST,
        host_port=_PORT,
        url="",
        max_out_len=4096,
        generation_kwargs=dict(temperature=0, ignore_eos=True),
    )
]