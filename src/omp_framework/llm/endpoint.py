"""OpenAI-compatible `/llm/v1` proxy endpoint (`GET /models` and `POST /chat/completions`) for omp.

Supports both non-streaming JSON and SSE streaming (`chat.completion.chunk` + `data: [DONE]`),
and attaches `x-should-retry: false` + `retry-after-ms: 600000` on errors so omp fails fast without
hanging on retry loops.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from omp_framework.contracts import ChatCompletionRequest, ChatCompletionResponse
from omp_framework.llm.backend import LlmBackend, OfflinePlanner

__all__ = [
    "DEFAULT_MODEL_ID",
    "MOUNT_PREFIX",
    "LlmEndpoint",
    "LlmRequestRecord",
    "create_llm_endpoint",
    "iter_chunks",
]

MOUNT_PREFIX: Final = "/llm/v1"
DEFAULT_MODEL_ID: Final = "omp-offline"
_SSE_HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_NO_RETRY: Final = {"x-should-retry": "false", "retry-after-ms": "600000"}


@dataclass(frozen=True, slots=True)
class LlmRequestRecord:
    seq: int
    received_at: datetime
    model: str
    tool_names: tuple[str, ...]
    stream: bool
    mode: str


def iter_chunks(
    response: ChatCompletionResponse,
    *,
    model: str,
    mode: str,
    chunk_chars: int = 64,
    include_usage: bool = False,
) -> Iterator[ str]:
    """Convert a `ChatCompletionResponse` into SSE `chat.completion.chunk` frames for omp."""
    comp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _frame(delta: dict[str, Any], finish_reason: str | None = None, choices_override: list[Any] | None = None, usage: dict[str, int] | None = None) -> str:
        payload: dict[str, Any] = {
            "id": comp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "mode": mode,
            "choices": (
                choices_override
                if choices_override is not None
                else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
            ),
            "usage": usage,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield _frame({"role": "assistant", "content": ""})

    text = response.content or ""
    for i in range(0, len(text), chunk_chars):
        yield _frame({"content": text[i : i + chunk_chars]})

    for idx, tc in enumerate(response.tool_calls):
        yield _frame(
            {
                "tool_calls": [
                    {
                        "index": idx,
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": ""},
                    }
                ]
            }
        )
        args_str = tc.function.arguments or "{}"
        for i in range(0, len(args_str), chunk_chars):
            yield _frame(
                {
                    "tool_calls": [
                        {
                            "index": idx,
                            "function": {"arguments": args_str[i : i + chunk_chars]},
                        }
                    ]
                }
            )

    finish = response.finish_reason or ("tool_calls" if response.tool_calls else "stop")
    yield _frame({}, finish_reason=finish)

    if include_usage:
        yield _frame({}, choices_override=[], usage=response.usage.model_dump())

    yield "data: [DONE]\n\n"


class LlmEndpoint:
    """FastAPI router wrapper for `/llm/v1`."""

    def __init__(
        self,
        backend: LlmBackend | None = None,
        *,
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        self.backend: LlmBackend = backend if backend is not None else OfflinePlanner()
        self._on_failure = on_failure
        self._seq = 0
        self._log: deque[LlmRequestRecord] = deque(maxlen=500)
        self.router = APIRouter()
        self.router.add_api_route("/models", self._get_models, methods=["GET"])
        self.router.add_api_route("/chat/completions", self._chat_completions, methods=["POST"])

    @property
    def mode(self) -> str:
        return self.backend.mode

    @property
    def request_log(self) -> tuple[LlmRequestRecord, ...]:
        return tuple(self._log)

    async def _get_models(self) -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "mode": self.mode,
                "data": [
                    {
                        "id": DEFAULT_MODEL_ID,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "omp-framework",
                    }
                ],
            }
        )

    async def _chat_completions(self, request: Request) -> Response:
        try:
            raw = await request.json()
            req = ChatCompletionRequest.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            return JSONResponse(
                status_code=400,
                headers=_NO_RETRY,
                content={
                    "error": {
                        "message": f"Invalid ChatCompletionRequest: {exc}",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_request",
                    }
                },
            )

        self._seq += 1
        self._log.append(
            LlmRequestRecord(
                seq=self._seq,
                received_at=datetime.now(UTC),
                model=req.model,
                tool_names=req.tool_names(),
                stream=req.stream,
                mode=self.mode,
            )
        )

        try:
            resp = await run_in_threadpool(self.backend.complete, req)
        except Exception as exc:
            if self._on_failure:
                self._on_failure("backend_error", str(exc))
            return JSONResponse(
                status_code=500,
                headers=_NO_RETRY,
                content={
                    "error": {
                        "message": f"LLM backend failed: {exc}",
                        "type": "server_error",
                        "param": None,
                        "code": "backend_error",
                    }
                },
            )

        if req.stream:
            include_usage = bool(req.stream_options and req.stream_options.get("include_usage"))
            return StreamingResponse(
                iter_chunks(resp, model=req.model, mode=self.mode, include_usage=include_usage),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        choice_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
        if resp.tool_calls:
            choice_msg["tool_calls"] = [tc.model_dump() for tc in resp.tool_calls]
        finish = resp.finish_reason or ("tool_calls" if resp.tool_calls else "stop")
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "mode": self.mode,
                "choices": [{"index": 0, "message": choice_msg, "finish_reason": finish}],
                "usage": resp.usage.model_dump(),
            }
        )


def create_llm_endpoint(
    backend: LlmBackend | None = None,
    *,
    on_failure: Callable[[str, str], None] | None = None,
) -> LlmEndpoint:
    return LlmEndpoint(backend=backend, on_failure=on_failure)
