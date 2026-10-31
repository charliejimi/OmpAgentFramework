"""omp RPC client: NDJSON over stdio, protocol v2 chunk reassembly, and Host Tool sub-protocol.

Implements the `oh-my-pi` (omp v18.11.2) RPC wire protocol:
- NDJSON frames over stdio (`ready` -> `negotiate_protocol` v2 -> `set_host_tools` -> `prompt` -> `host_tool_call` / `host_tool_result` -> `prompt_result` -> `session_settled`).
- Automatic `rpc_chunk` base64 validation and reassembly up to `maxReassembledFrameBytes` (64 MiB).
- Dedicated worker thread for `HostToolHandler` so `host_tool_cancel`, timeouts, and aborts are processed immediately while a tool is running.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import IO, Any, Final, Literal

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_REASSEMBLED_FRAME_BYTES",
    "PROTOCOL_V2",
    "FrameDecoder",
    "HostToolCall",
    "HostToolExchange",
    "HostToolHandler",
    "HostToolResult",
    "PromptOutcome",
    "ReadyInfo",
    "RpcAborted",
    "RpcClient",
    "RpcClosedError",
    "RpcCommandError",
    "RpcError",
    "RpcEvent",
    "RpcProtocolError",
]

DEFAULT_MAX_FRAME_BYTES: Final = 1024 * 1024
DEFAULT_MAX_REASSEMBLED_FRAME_BYTES: Final = 64 * 1024 * 1024
PROTOCOL_V2: Final = 2
_STDOUT_LINE_LIMIT: Final = DEFAULT_MAX_REASSEMBLED_FRAME_BYTES + 1


class RpcError(Exception):
    """Base error for omp RPC failures; `reason` is the abort reason code."""

    reason: str = "protocol_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class RpcProtocolError(RpcError):
    reason = "protocol_error"


class RpcClosedError(RpcError):
    reason = "process_exit"


class RpcCommandError(RpcError):
    reason = "command_failed"

    def __init__(self, command: str, error: str, code: str | None = None) -> None:
        super().__init__(f"omp command {command} failed: {error}" + (f" ({code})" if code else ""))
        self.command = command
        self.error = error
        self.code = code


class RpcAborted(RpcError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReadyInfo:
    protocol_version: int
    supported_versions: tuple[int, ...]
    max_frame_bytes: int
    max_reassembled_frame_bytes: int
    frame: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class RpcEvent:
    type: str
    frame: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class HostToolCall:
    id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    seq: int
    cancel_event: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)
    on_progress: Callable[[str], None] | None = field(default=None, compare=False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def report_progress(self, text: str) -> None:
        """Send a streaming `host_tool_update` partialResult frame back to omp while the tool is running."""
        if not self.cancelled and self.on_progress is not None:
            self.on_progress(text)


@dataclass(frozen=True, slots=True)
class HostToolResult:
    text: str
    is_error: bool = False
    details: Mapping[str, Any] | None = None

    @classmethod
    def error(cls, text: str) -> HostToolResult:
        return cls(text, is_error=True)

    def to_frame(self, call_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": self.text}]}
        if self.details is not None:
            result["details"] = dict(self.details)
        frame: dict[str, Any] = {"type": "host_tool_result", "id": call_id, "result": result}
        if self.is_error:
            frame["isError"] = True
        return frame


HostToolHandler = Callable[[HostToolCall], HostToolResult | str]
ExchangeStatus = Literal["ok", "error", "cancelled", "refused"]


@dataclass(frozen=True, slots=True)
class HostToolExchange:
    call: HostToolCall
    result: HostToolResult | None
    status: ExchangeStatus
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class PromptOutcome:
    id: str
    agent_invoked: bool
    status: str
    error: Mapping[str, Any] | None
    session_settled: bool
    events: tuple[RpcEvent, ...] = field(repr=False)
    tool_calls: tuple[HostToolExchange, ...]
    elapsed_s: float


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _snippet(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars)"


def _left(limit: float | None) -> float | None:
    return None if limit is None else max(0.0, limit - time.monotonic())


@dataclass(slots=True)
class _PendingChunks:
    chunk_id: str
    count: int
    byte_length: int
    next_index: int = 0
    received: int = 0
    parts: list[bytes] = field(default_factory=list)


class FrameDecoder:
    """Decodes NDJSON lines from omp stdout and reassembles protocol v2 `rpc_chunk` sequences."""

    def __init__(
        self,
        *,
        max_reassembled_bytes: int = DEFAULT_MAX_REASSEMBLED_FRAME_BYTES,
        accept_chunks: bool = False,
    ) -> None:
        self.max_reassembled_bytes = max_reassembled_bytes
        self.accept_chunks = accept_chunks
        self.reassembled = 0
        self._pending: _PendingChunks | None = None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def feed_line(self, line: bytes) -> dict[str, Any] | None:
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RpcProtocolError(f"omp stdout is not valid UTF-8: {exc}") from exc
        text = text.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RpcProtocolError(f"omp stdout is not valid JSON: {_snippet(text)}") from exc
        return self.push(value)

    def push(self, value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            raise RpcProtocolError("RPC frame must be a JSON object")
        if value.get("type") != "rpc_chunk":
            if self._pending is not None:
                raise RpcProtocolError(f"rpc_chunk sequence {self._pending.chunk_id!r} interrupted by another frame")
            return value
        if not self.accept_chunks:
            raise RpcProtocolError("Received rpc_chunk before negotiating protocol v2")

        chunk_id = value.get("chunkId")
        index = value.get("index")
        count = value.get("count")
        byte_length = value.get("byteLength")
        data = value.get("data")
        if not (
            isinstance(chunk_id, str)
            and chunk_id
            and _is_int(index)
            and _is_int(count)
            and _is_int(byte_length)
            and isinstance(data, str)
            and data
        ):
            raise RpcProtocolError("Invalid rpc_chunk fields (chunkId, index, count, byteLength, or data)")
        if not (0 <= index < count <= byte_length):
            raise RpcProtocolError(f"Invalid rpc_chunk bounds: index={index}, count={count}, byteLength={byte_length}")
        if byte_length > self.max_reassembled_bytes:
            raise RpcProtocolError(
                f"rpc_chunk byteLength {byte_length} exceeds max {self.max_reassembled_bytes}"
            )
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RpcProtocolError(f"rpc_chunk {chunk_id!r} data is not valid base64") from exc

        pending = self._pending
        if pending is None:
            if index != 0:
                raise RpcProtocolError(f"rpc_chunk {chunk_id!r} must start at index 0 (got {index})")
            pending = self._pending = _PendingChunks(chunk_id, count, byte_length)
        elif (chunk_id, count, byte_length) != (pending.chunk_id, pending.count, pending.byte_length):
            raise RpcProtocolError(f"rpc_chunk {pending.chunk_id!r} interleaved with {chunk_id!r}")
        elif index != pending.next_index:
            raise RpcProtocolError(f"rpc_chunk {chunk_id!r} expected index {pending.next_index}, got {index}")

        pending.parts.append(chunk)
        pending.received += len(chunk)
        pending.next_index += 1
        if pending.received > pending.byte_length:
            raise RpcProtocolError(f"rpc_chunk {chunk_id!r} exceeded declared byteLength {pending.byte_length}")
        if pending.next_index < pending.count:
            return None
        self._pending = None
        if pending.received != pending.byte_length:
            raise RpcProtocolError(
                f"rpc_chunk {chunk_id!r} received {pending.received} bytes, expected {pending.byte_length}"
            )
        try:
            frame = json.loads(b"".join(pending.parts).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcProtocolError(f"Reassembled rpc_chunk {chunk_id!r} is not valid UTF-8 JSON") from exc
        if not isinstance(frame, dict):
            raise RpcProtocolError(f"Reassembled rpc_chunk {chunk_id!r} is not a JSON object")
        self.reassembled += 1
        return frame


def _ready_info(frame: Mapping[str, Any]) -> ReadyInfo:
    version = frame.get("protocolVersion")
    version = version if _is_int(version) else 1
    raw_versions = frame.get("supportedProtocolVersions")
    supported = tuple(v for v in raw_versions if _is_int(v)) if isinstance(raw_versions, list) else ()
    max_frame = frame.get("maxFrameBytes")
    max_reassembled = frame.get("maxReassembledFrameBytes")
    return ReadyInfo(
        protocol_version=version,
        supported_versions=supported or (version,),
        max_frame_bytes=max_frame if _is_int(max_frame) and max_frame > 0 else DEFAULT_MAX_FRAME_BYTES,
        max_reassembled_frame_bytes=(
            max_reassembled
            if _is_int(max_reassembled) and max_reassembled > 0
            else DEFAULT_MAX_REASSEMBLED_FRAME_BYTES
        ),
        frame=frame,
    )


def _encode(frame: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RpcProtocolError(f"Frame {frame.get('type')} cannot be JSON-encoded: {exc}") from exc
    return text.encode("utf-8") + b"\n"


class RpcClient:
    """omp RPC client over stdio."""

    def __init__(
        self,
        stdin: IO[bytes],
        stdout: IO[bytes],
        *,
        handler: HostToolHandler | None = None,
        on_event: Callable[[RpcEvent], object] | None = None,
        max_tool_calls: int | None = None,
        name: str = "omp",
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.deadline: float | None = None
        self._stdin = stdin
        self._stdout = stdout
        self._handler = handler
        self._on_event = on_event
        self._name = name
        self._decoder = FrameDecoder()
        self._inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._write_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._stdin_closed = False
        self._eof = False
        self._failure: RpcError | None = None
        self._ready: ReadyInfo | None = None
        self._events: list[RpcEvent] = []
        self._exchanges: list[HostToolExchange] = []
        self._calls: dict[str, HostToolCall] = {}
        self._tool_count = 0
        self._tool_queue: queue.Queue[HostToolCall | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._reader = threading.Thread(target=self._read_loop, name=f"{name}-rpc-stdout", daemon=True)
        self._reader.start()

    @property
    def ready(self) -> ReadyInfo | None:
        return self._ready

    @property
    def protocol_version(self) -> int:
        return self._ready.protocol_version if self._ready is not None else 1

    @property
    def events(self) -> tuple[RpcEvent, ...]:
        return tuple(self._events)

    @property
    def tool_calls(self) -> tuple[HostToolExchange, ...]:
        with self._record_lock:
            return tuple(self._exchanges)

    @property
    def tool_call_count(self) -> int:
        return self._tool_count

    @property
    def reassembled_frames(self) -> int:
        return self._decoder.reassembled

    @property
    def eof(self) -> bool:
        return self._eof

    def connect(self, timeout: float | None = 60.0, *, negotiate: bool = True) -> ReadyInfo:
        limit = None if timeout is None else time.monotonic() + timeout
        ready = self.wait_ready(_left(limit))
        if negotiate and PROTOCOL_V2 in ready.supported_versions and ready.protocol_version != PROTOCOL_V2:
            self.negotiate(_left(limit))
        assert self._ready is not None
        return self._ready

    def wait_ready(self, timeout: float | None = 60.0) -> ReadyInfo:
        if self._ready is None:
            self._pump(lambda frame: frame.get("type") == "ready", timeout, "ready")
        assert self._ready is not None
        return self._ready

    def negotiate(self, timeout: float | None = 30.0) -> int:
        ready = self.wait_ready(timeout)
        if PROTOCOL_V2 not in ready.supported_versions:
            raise RpcProtocolError(f"omp does not support protocol v2 (supported={list(ready.supported_versions)})")
        self._decoder.accept_chunks = True
        try:
            data = self.request("negotiate_protocol", timeout=timeout, protocolVersion=PROTOCOL_V2)
        except BaseException:
            self._decoder.accept_chunks = False
            raise
        version = data.get("protocolVersion") if isinstance(data, Mapping) else None
        if version != PROTOCOL_V2:
            self._decoder.accept_chunks = False
            raise RpcProtocolError(f"negotiate_protocol returned version {version!r}, expected 2")
        self._ready = replace(ready, protocol_version=PROTOCOL_V2)
        return PROTOCOL_V2

    def request(self, command: str, *, timeout: float | None = 30.0, **fields: Any) -> Any:
        request_id = f"omp-{next(self._ids)}"
        self._send({**fields, "id": request_id, "type": command})
        response = self._pump(
            lambda frame: frame.get("type") == "response" and frame.get("id") == request_id,
            timeout,
            f"response for {command}",
        )
        assert response is not None
        if response.get("success") is not True:
            code = response.get("code")
            raise RpcCommandError(command, str(response.get("error") or "unknown error"), code if isinstance(code, str) else None)
        return response.get("data")

    def set_host_tools(self, tools: Sequence[Mapping[str, Any]], *, timeout: float | None = 30.0) -> tuple[str, ...]:
        data = self.request("set_host_tools", timeout=timeout, tools=[dict(tool) for tool in tools])
        names = data.get("toolNames") if isinstance(data, Mapping) else None
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise RpcProtocolError("set_host_tools response missing toolNames")
        return tuple(names)

    def get_state(self, *, timeout: float | None = 30.0) -> dict[str, Any]:
        data = self.request("get_state", timeout=timeout)
        if not isinstance(data, dict):
            raise RpcProtocolError("get_state response missing data")
        return data

    def last_assistant_text(self, *, timeout: float | None = 30.0) -> str | None:
        data = self.request("get_last_assistant_text", timeout=timeout)
        text = data.get("text") if isinstance(data, Mapping) else None
        return text if isinstance(text, str) else None

    def prompt(self, message: str, *, timeout: float | None = None, settle_grace_s: float = 5.0) -> PromptOutcome:
        started = time.monotonic()
        limit = None if timeout is None else started + timeout
        first_event = len(self._events)
        with self._record_lock:
            first_call = len(self._exchanges)
        prompt_id = f"omp-prompt-{next(self._ids)}"
        self._send({"id": prompt_id, "type": "prompt", "message": message})
        ack = self._pump(
            lambda frame: frame.get("type") == "response" and frame.get("id") == prompt_id,
            _left(limit),
            "prompt ack",
        )
        assert ack is not None
        if ack.get("success") is not True:
            code = ack.get("code")
            raise RpcCommandError("prompt", str(ack.get("error") or "unknown error"), code if isinstance(code, str) else None)
        ack_data = ack.get("data")
        if isinstance(ack_data, Mapping) and ack_data.get("agentInvoked") is False:
            return self._outcome(prompt_id, False, "completed", None, True, first_event, first_call, started)

        result = self._pump(
            lambda frame: frame.get("type") == "prompt_result" and frame.get("id") == prompt_id,
            _left(limit),
            "prompt_result",
        )
        assert result is not None
        agent_invoked = result.get("agentInvoked") is True
        settled = result.get("sessionSettled") is True
        if agent_invoked:
            is_settled_frame: Callable[[Mapping[str, Any]], bool] = lambda frame: frame.get("type") == "session_settled"
            if settled:
                grace = settle_grace_s if limit is None else min(settle_grace_s, max(0.0, limit - time.monotonic()))
                self._pump(is_settled_frame, grace, "session_settled", optional=True)
            else:
                self._pump(is_settled_frame, _left(limit), "session_settled")
                settled = True
        status = result.get("status")
        error = result.get("error")
        return self._outcome(
            prompt_id,
            agent_invoked,
            status if isinstance(status, str) else "completed",
            error if isinstance(error, Mapping) else None,
            settled,
            first_event,
            first_call,
            started,
        )

    def cancel(self, reason: str = "cancelled", detail: str = "Execution cancelled by caller") -> None:
        self._cancel_pending_calls()
        self._inbox.put(("cancel", RpcAborted(reason, detail)))

    def send_abort(self) -> bool:
        try:
            self._send({"id": f"omp-abort-{next(self._ids)}", "type": "abort"}, force=True)
        except RpcError:
            return False
        return True

    def close_stdin(self) -> None:
        with self._write_lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            try:
                self._stdin.close()
            except OSError:
                pass

    def close(self, timeout: float | None = 5.0) -> bool:
        self.close_stdin()
        self._cancel_pending_calls()
        self._tool_queue.put(None)
        limit = None if timeout is None else time.monotonic() + timeout
        while not self._eof:
            wait = _left(limit)
            if wait is not None and wait <= 0:
                return False
            try:
                kind, payload = self._inbox.get(timeout=wait)
            except queue.Empty:
                return False
            if kind == "eof":
                self._eof = True
            elif kind == "frame":
                self._record(payload, serve_tools=False)
        return True

    def wait_reader(self, timeout: float | None = None) -> bool:
        self._reader.join(timeout)
        return not self._reader.is_alive()

    def _max_frame_bytes(self) -> int:
        return self._ready.max_frame_bytes if self._ready is not None else DEFAULT_MAX_FRAME_BYTES

    def _send(self, frame: Mapping[str, Any], *, force: bool = False) -> None:
        if not force and self._failure is not None:
            raise self._failure
        self._write(_encode(frame), str(frame.get("type")))

    def _write(self, data: bytes, kind: str) -> None:
        limit = self._max_frame_bytes()
        if len(data) > limit:
            raise RpcProtocolError(f"Frame {kind} ({len(data)} bytes) exceeds maxFrameBytes ({limit} bytes)")
        with self._write_lock:
            if self._stdin_closed:
                raise RpcClosedError(f"omp stdin is closed; cannot send {kind}")
            try:
                self._stdin.write(data)
                self._stdin.flush()
            except (OSError, ValueError) as exc:
                raise RpcClosedError(f"Failed writing {kind} to omp stdin: {exc}") from exc

    def _read_loop(self) -> None:
        broken = False
        try:
            while True:
                line = self._stdout.readline(_STDOUT_LINE_LIMIT)
                if not line:
                    break
                if broken:
                    continue
                if len(line) >= _STDOUT_LINE_LIMIT and not line.endswith(b"\n"):
                    broken = True
                    self._inbox.put(("error", RpcProtocolError(f"omp stdout line exceeded {_STDOUT_LINE_LIMIT - 1} bytes")))
                    continue
                try:
                    frame = self._decoder.feed_line(line)
                except RpcProtocolError as exc:
                    broken = True
                    self._inbox.put(("error", exc))
                    continue
                if frame is not None:
                    self._inbox.put(("frame", frame))
        except (OSError, ValueError):
            pass
        finally:
            self._inbox.put(("eof", None))

    def _pump(
        self,
        done: Callable[[Mapping[str, Any]], bool],
        timeout: float | None,
        label: str,
        *,
        optional: bool = False,
    ) -> Mapping[str, Any] | None:
        if self._failure is not None:
            raise self._failure
        wait_limit = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            now = time.monotonic()
            deadline = self.deadline
            if deadline is not None and now >= deadline:
                raise self._fail(RpcAborted("timeout", f"Deadline exceeded while waiting for {label}"))
            if wait_limit is not None and now >= wait_limit:
                if optional:
                    return None
                raise self._fail(RpcAborted("timeout", f"Timed out waiting for {label} ({timeout:g}s)"))
            limits = [t for t in (deadline, wait_limit) if t is not None]
            try:
                kind, payload = self._inbox.get(timeout=max(0.0, min(limits) - now) if limits else None)
            except queue.Empty:
                continue
            if kind == "frame":
                frame = self._record(payload, serve_tools=True)
                if done(frame):
                    return frame
            elif kind == "eof":
                self._eof = True
                raise self._fail(RpcClosedError(f"omp closed stdout while waiting for {label}"))
            elif kind in ("error", "cancel"):
                raise self._fail(payload)

    def _fail(self, error: RpcError) -> RpcError:
        if self._failure is None:
            self._failure = error
        self._cancel_pending_calls()
        return self._failure

    def _cancel_pending_calls(self) -> None:
        for call in self._calls.copy().values():
            call.cancel_event.set()

    def _record(self, frame: Mapping[str, Any], *, serve_tools: bool) -> Mapping[str, Any]:
        kind = frame.get("type")
        if kind == "response":
            return frame
        event = RpcEvent(str(kind), frame)
        self._events.append(event)
        if kind == "ready" and self._ready is None:
            self._ready = _ready_info(frame)
            self._decoder.max_reassembled_bytes = self._ready.max_reassembled_frame_bytes
        if self._on_event is not None:
            self._on_event(event)
        if kind == "host_tool_call" and serve_tools:
            self._on_host_tool_call(frame)
        elif kind == "host_tool_cancel":
            target = frame.get("targetId")
            call = self._calls.get(target) if isinstance(target, str) else None
            if call is not None:
                call.cancel_event.set()
        return frame

    def _outcome(
        self,
        prompt_id: str,
        agent_invoked: bool,
        status: str,
        error: Mapping[str, Any] | None,
        settled: bool,
        first_event: int,
        first_call: int,
        started: float,
    ) -> PromptOutcome:
        with self._record_lock:
            calls = tuple(self._exchanges[first_call:])
        return PromptOutcome(
            id=prompt_id,
            agent_invoked=agent_invoked,
            status=status,
            error=error,
            session_settled=settled,
            events=tuple(self._events[first_event:]),
            tool_calls=calls,
            elapsed_s=time.monotonic() - started,
        )

    def send_tool_update(self, call_id: str, text: str) -> bool:
        """Send an inbound `host_tool_update` streaming progress frame to omp (`docs/rpc.md`)."""
        frame = {
            "type": "host_tool_update",
            "id": call_id,
            "partialResult": {"content": [{"type": "text", "text": text}]},
        }
        try:
            self._write(_encode(frame), "host_tool_update")
            return True
        except RpcError:
            return False

    def _on_host_tool_call(self, frame: Mapping[str, Any]) -> None:
        request_id = frame.get("id")
        tool_name = frame.get("toolName")
        if not isinstance(request_id, str) or not request_id or not isinstance(tool_name, str) or not tool_name:
            raise self._fail(RpcProtocolError("host_tool_call missing id or toolName"))
        tool_call_id = frame.get("toolCallId")
        arguments = frame.get("arguments")
        self._tool_count += 1
        call = HostToolCall(
            id=request_id,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else "",
            tool_name=tool_name,
            arguments=arguments if isinstance(arguments, dict) else {},
            seq=self._tool_count,
            on_progress=lambda text, cid=request_id: self.send_tool_update(cid, text),
        )
        limit = self.max_tool_calls
        if limit is not None and self._tool_count > limit:
            refusal = HostToolResult.error(f"Exceeded max_tool_calls limit ({limit}); refusing {tool_name}")
            self._deliver(call, refusal, "refused", 0.0)
            raise self._fail(
                RpcAborted("tool_limit", f"Exceeded max_tool_calls limit ({limit}) on call #{self._tool_count} ({tool_name})")
            )
        if not isinstance(arguments, dict):
            self._deliver(call, HostToolResult.error("Host Tool arguments must be a JSON object"), "error", 0.0)
            return
        if self._handler is None:
            self._deliver(call, HostToolResult.error(f"No Host Tool handler registered for {tool_name}"), "error", 0.0)
            return
        self._calls[request_id] = call
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name=f"{self._name}-host-tools", daemon=True)
            self._worker.start()
        self._tool_queue.put(call)

    def _work(self) -> None:
        while True:
            call = self._tool_queue.get()
            if call is None:
                return
            try:
                self._run_tool(call)
            finally:
                self._calls.pop(call.id, None)

    def _run_tool(self, call: HostToolCall) -> None:
        if call.cancelled:
            self._record_exchange(HostToolExchange(call, None, "cancelled", 0.0))
            return
        assert self._handler is not None
        started = time.monotonic()
        try:
            output = self._handler(call)
        except Exception as exc:
            result = HostToolResult.error(f"{type(exc).__name__}: {exc}")
        else:
            if isinstance(output, HostToolResult):
                result = output
            elif isinstance(output, str):
                result = HostToolResult(output)
            else:
                result = HostToolResult.error(f"Invalid Host Tool handler return type: {type(output).__name__}")
        elapsed = time.monotonic() - started
        if call.cancelled:
            self._record_exchange(HostToolExchange(call, result, "cancelled", elapsed))
            return
        self._deliver(call, result, "error" if result.is_error else "ok", elapsed)

    def _deliver(self, call: HostToolCall, result: HostToolResult, status: ExchangeStatus, elapsed: float) -> None:
        try:
            data = _encode(result.to_frame(call.id))
        except RpcProtocolError as exc:
            result, status = HostToolResult.error(f"Tool result could not be encoded as JSON: {exc.detail}"), "error"
            data = _encode(result.to_frame(call.id))
        limit = self._max_frame_bytes()
        if len(data) > limit:
            result, status = HostToolResult.error(
                f"Tool result ({len(data)} bytes) exceeds maxFrameBytes ({limit} bytes)"
            ), "error"
            data = _encode(result.to_frame(call.id))
        self._record_exchange(HostToolExchange(call, result, status, elapsed))
        try:
            self._write(data, "host_tool_result")
        except RpcClosedError:
            pass

    def _record_exchange(self, exchange: HostToolExchange) -> None:
        with self._record_lock:
            self._exchanges.append(exchange)
