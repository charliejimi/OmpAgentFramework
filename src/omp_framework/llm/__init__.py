"""Pluggable LLM router and `/llm/v1` proxy for omp."""

from omp_framework.llm.backend import (
    AGENT_MARKER_PREFIX,
    BACKEND_ENV,
    LlmBackend,
    OfflinePlanner,
    agent_marker,
    identify_agent,
    load_backend,
    make_text_response,
    make_tool_call_response,
)
from omp_framework.llm.endpoint import (
    DEFAULT_MODEL_ID,
    MOUNT_PREFIX,
    LlmEndpoint,
    LlmRequestRecord,
    create_llm_endpoint,
    iter_chunks,
)

__all__ = [
    "AGENT_MARKER_PREFIX",
    "BACKEND_ENV",
    "DEFAULT_MODEL_ID",
    "MOUNT_PREFIX",
    "LlmBackend",
    "LlmEndpoint",
    "LlmRequestRecord",
    "OfflinePlanner",
    "agent_marker",
    "create_llm_endpoint",
    "identify_agent",
    "iter_chunks",
    "load_backend",
    "make_text_response",
    "make_tool_call_response",
]
