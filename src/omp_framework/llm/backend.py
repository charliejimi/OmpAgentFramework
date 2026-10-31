"""Pluggable LLM Backend & Deterministic Offline Planner Base.

Allows `omp` to talk to `/llm/v1` while the host routes completions to:
1. A deterministic `OfflinePlanner` (rule-based multi-turn tool-call state machine for tests & air-gapped fallback),
2. Any custom Python `LlmBackend` specified via `OMP_LLM_BACKEND=<module>:<Class>`,
3. Or external OpenAI-compatible providers / `omp` extensions configured in `agents/omp/config.yml`.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol, runtime_checkable

from omp_framework.contracts import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ToolCallFunction,
    ToolCallSpec,
    Usage,
)

__all__ = [
    "AGENT_MARKER_PREFIX",
    "BACKEND_ENV",
    "LlmBackend",
    "OfflinePlanner",
    "agent_marker",
    "identify_agent",
    "load_backend",
    "make_tool_call_response",
    "make_text_response",
]

BACKEND_ENV: Final = "OMP_LLM_BACKEND"
AGENT_MARKER_PREFIX: Final = "omp-agent:"
_MARKER_RE: Final = re.compile(r"^omp-agent:\s*([a-zA-Z0-9_-]+)\s*$", re.MULTILINE)


def agent_marker(agent_id: str) -> str:
    return f"{AGENT_MARKER_PREFIX} {agent_id}"


def identify_agent(request: ChatCompletionRequest) -> str | None:
    """Identify which agent profile is executing from the `omp-agent: <id>` marker in system messages."""
    for msg in request.messages:
        if msg.role in ("system", "developer"):
            text = msg.text_content()
            matches = _MARKER_RE.findall(text)
            if len(matches) == 1:
                return matches[0]
    return None


@runtime_checkable
class LlmBackend(Protocol):
    """Protocol for any pluggable LLM backend serving `/llm/v1`."""

    @property
    def mode(self) -> str: ...

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...


def make_tool_call_response(
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    call_id: str = "call_1",
    content: str = "",
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        content=content,
        tool_calls=[
            ToolCallSpec(
                id=call_id,
                function=ToolCallFunction(
                    name=tool_name,
                    arguments=json.dumps(dict(arguments or {}), ensure_ascii=False),
                ),
            )
        ],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=64, completion_tokens=32, total_tokens=96),
    )


def make_text_response(text: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(prompt_tokens=64, completion_tokens=max(1, len(text) // 4), total_tokens=64 + max(1, len(text) // 4)),
    )


AgentTurnPlanner = Callable[[ChatCompletionRequest, list[tuple[str, dict[str, Any]]]], ChatCompletionResponse]


class OfflinePlanner:
    """Deterministic multi-turn planner that speaks OpenAI tool-calls for zero-key testing and air-gapped operation.

    Domain packs can register per-agent turn planners via `register_agent_planner(agent_id, fn)` or rely on
    the built-in generic fallback that calls the first declared read-only tool and summarizes the result.
    """

    def __init__(self) -> None:
        self._planners: dict[str, AgentTurnPlanner] = {}

    @property
    def mode(self) -> str:
        return "offline"

    def register(self, agent_id: str, planner: AgentTurnPlanner) -> None:
        self._planners[agent_id] = planner

    @staticmethod
    def extract_tool_history(request: ChatCompletionRequest) -> list[tuple[str, dict[str, Any]]]:
        """Return ordered `(tool_name, parsed_json_result)` pairs from prior turns in `request.messages`."""
        call_id_to_name: dict[str, str] = {}
        history: list[tuple[str, dict[str, Any]]] = []
        for msg in request.messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id")
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    name = fn.get("name") if isinstance(fn, dict) else None
                    if isinstance(tc_id, str) and isinstance(name, str):
                        call_id_to_name[tc_id] = name
            elif msg.role == "tool":
                name = msg.name or (call_id_to_name.get(msg.tool_call_id or "") if msg.tool_call_id else None) or "unknown"
                raw = msg.text_content()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                history.append((name, parsed if isinstance(parsed, dict) else {"value": parsed}))
        return history

    def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        agent_id = identify_agent(request)
        history = self.extract_tool_history(request)
        if agent_id and agent_id in self._planners:
            return self._planners[agent_id](request, history)

        # Generic fallback: if no tool called yet and tools are declared, call the first tool with {}
        declared = request.tool_names()
        if not history and declared:
            return make_tool_call_response(declared[0], {}, call_id="call_generic_1")
        if history:
            last_tool, last_res = history[-1]
            if last_res.get("error"):
                return make_text_response(f"Tool query failed ({last_tool}): {last_res.get('error')}")
            return make_text_response(
                f"[{agent_id or 'agent'}] Completed analysis using `{last_tool}`. "
                f"Result summary: {json.dumps(last_res, ensure_ascii=False)[:400]}"
            )
        user_text = next((m.text_content() for m in reversed(request.messages) if m.role == "user"), "")
        return make_text_response(f"[{agent_id or 'agent'}] Processed request: {user_text[:200]}")


def load_backend(default: LlmBackend | None = None) -> LlmBackend:
    """Load `LlmBackend` from `OMP_LLM_BACKEND=<module>:<Class>` or return `default` (`OfflinePlanner`)."""
    spec = os.environ.get(BACKEND_ENV, "").strip()
    if not spec:
        return default if default is not None else OfflinePlanner()
    if ":" not in spec:
        raise ValueError(f"{BACKEND_ENV} must be '<module>:<Class>', got {spec!r}")
    mod_name, cls_name = spec.split(":", 1)
    module = importlib.import_module(mod_name)
    cls = getattr(module, cls_name)
    instance = cls()
    if not isinstance(instance, LlmBackend):
        raise TypeError(f"{spec} does not implement LlmBackend protocol")
    return instance
