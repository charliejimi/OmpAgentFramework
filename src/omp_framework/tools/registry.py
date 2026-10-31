"""Dynamic Host Tool Registry, ScopeContext Injection, and Gate Hook.

Provides a clean decorator/plugin registration API so any Domain Pack can register Host Tools
without modifying framework internals:

- Every tool specifies `name`, `label`, `description`, `domain`, `rule` (`read_only`, `report_draft`,
  `rule_draft`, `ticket`, `intent`, `expert_memory`), and a Pydantic `args_model`.
- Generates JSON Schema for `omp`'s `set_host_tools` RPC command automatically.
- Intercepts every tool call in `ToolSession.call(...)`:
  1. Calls `PolicyGate.authorize(principal, action, gate_ctx)`.
  2. Verifies the tool is in the calling agent's allowed tool list and respects `ScopeContext.read_only`.
  3. Validates arguments against `args_model` (rejecting unknown or scope-spoofing arguments).
  4. Invokes the handler with `ToolInvocation(scope, args, call_id, decision, session)` so the handler
     always uses the host-injected `ScopeContext`.
  5. Extracts `SourceRef` provenance into `Citation` objects and formats the JSON response for `omp`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from omp_framework.contracts import (
    Action,
    Citation,
    ContractModel,
    Decision,
    GateContext,
    Principal,
    ScopeContext,
    SourceRef,
    ToolCallRecord,
)
from omp_framework.rpc.client import HostToolCall, HostToolResult

__all__ = [
    "NoArgs",
    "ToolError",
    "ToolErrorKind",
    "ToolHandler",
    "ToolInvocation",
    "ToolOutput",
    "ToolRegistry",
    "ToolResponse",
    "ToolRule",
    "ToolSession",
    "ToolSpec",
]

ToolRule = Literal[
    "read_only",
    "report_draft",
    "rule_draft",
    "ticket",
    "intent",
    "expert_memory",
]

ToolErrorKind = Literal[
    "invalid_name",
    "not_allowed",
    "gate_error",
    "denied",
    "invalid_arguments",
    "not_registered",
    "failed",
]

_DENY_KINDS = ("invalid_name", "not_allowed", "gate_error", "denied")


class NoArgs(ContractModel):
    """Empty parameter model for tools that take no LLM-supplied parameters (only host-injected ScopeContext)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolError(Exception):
    """Expected tool execution error returned to omp with `isError: true`."""

    def __init__(self, message: str, *, kind: ToolErrorKind = "failed") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """Return value from a registered `ToolHandler`."""

    data: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    sources: list[tuple[str, SourceRef]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Metadata and schema definition for a Host Tool."""

    name: str
    label: str
    description: str
    domain: str
    rule: ToolRule = "read_only"
    args_model: type[BaseModel] = NoArgs

    @property
    def mutates(self) -> bool:
        return self.rule != "read_only"

    def rpc_spec(self) -> dict[str, Any]:
        """Return the tool declaration format expected by omp `set_host_tools`."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "parameters": schema,
        }


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Context passed to a tool handler when invoked by an agent."""

    tool: ToolSpec
    scope: ScopeContext
    args: Any
    call_id: str
    decision: Decision
    session: ToolSession


ToolHandler = Callable[[ToolInvocation], ToolOutput | dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolResponse:
    is_error: bool
    text: str
    data: dict[str, Any] | None
    error_kind: ToolErrorKind | None
    error: str | None
    citations: list[Citation]
    decision: Decision | None
    record: ToolCallRecord

    def to_host_tool_result(self) -> HostToolResult:
        return HostToolResult(text=self.text, is_error=self.is_error)


class ToolRegistry:
    """Dynamic registry of Host Tools and their handlers."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Register a `ToolSpec` and its handler (usable as a direct call or `@registry.register(spec)` decorator)."""
        self._specs[spec.name] = spec
        if handler is not None:
            self._handlers[spec.name] = handler
            return handler

        def _decorator(fn: ToolHandler) -> ToolHandler:
            self._handlers[spec.name] = fn
            return fn

        return _decorator

    def tool(
        self,
        name: str,
        *,
        label: str,
        description: str,
        domain: str = "general",
        rule: ToolRule = "read_only",
        args_model: type[BaseModel] = NoArgs,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Ergonomic decorator to define and register a Host Tool in one step."""
        spec = ToolSpec(
            name=name,
            label=label,
            description=description,
            domain=domain,
            rule=rule,
            args_model=args_model,
        )
        return self.register(spec)

    def get_spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def get_handler(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def all_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def declarations_for(self, tool_names: Sequence[str], *, read_only: bool = False) -> list[dict[str, Any]]:
        decls: list[dict[str, Any]] = []
        for name in tool_names:
            spec = self._specs.get(name)
            if spec is None:
                continue
            if read_only and spec.mutates:
                continue
            decls.append(spec.rpc_spec())
        return decls

    def session(
        self,
        *,
        agent_id: str,
        agent_domain: str,
        allowed_tools: Sequence[str],
        scope: ScopeContext,
        gate: Any,
    ) -> ToolSession:
        return ToolSession(
            registry=self,
            agent_id=agent_id,
            agent_domain=agent_domain,
            allowed_tools=tuple(allowed_tools),
            scope=scope,
            gate=gate,
        )


class ToolSession:
    """Per-run Host Tool execution session bound to a fixed `ScopeContext`, `Agent`, and `PolicyGate`."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        agent_id: str,
        agent_domain: str,
        allowed_tools: tuple[str, ...],
        scope: ScopeContext,
        gate: Any,
    ) -> None:
        self.registry = registry
        self.agent_id = agent_id
        self.agent_domain = agent_domain
        self.allowed_tools = allowed_tools
        self.scope = scope
        self.gate = gate
        self.state: dict[str, Any] = {}
        self._records: list[ToolCallRecord] = []
        self._citations: list[Citation] = []
        self._seq = 0

    @property
    def records(self) -> tuple[ToolCallRecord, ...]:
        return tuple(self._records)

    @property
    def citations(self) -> tuple[Citation, ...]:
        return tuple(self._citations)

    def declarations(self) -> list[dict[str, Any]]:
        return self.registry.declarations_for(self.allowed_tools, read_only=self.scope.read_only)

    def handle_rpc_call(self, call: HostToolCall) -> HostToolResult:
        resp = self.call(call.tool_name, call.arguments, call_id=call.tool_call_id or call.id)
        return resp.to_host_tool_result()

    def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        call_id: str = "",
    ) -> ToolResponse:
        started = time.monotonic()
        self._seq += 1
        seq = self._seq
        cid = call_id or f"call_{seq}"
        raw_args = dict(arguments or {})

        spec = self.registry.get_spec(tool_name)
        domain = spec.domain if spec is not None else "unknown"
        principal = Principal.for_agent(self.agent_id, domain=self.agent_domain)
        action = Action.tool(tool_name, domain=domain)
        gate_ctx = GateContext(scope=self.scope, arguments=raw_args)

        # 1. PolicyGate authorization
        try:
            decision: Decision = self.gate.authorize(principal, action, gate_ctx)
        except Exception as exc:
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="gate_error",
                error=f"Gate authorization failed: {exc}",
                decision=None,
                started=started,
            )

        if spec is None:
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="invalid_name",
                error=f"Unknown Host Tool: {tool_name}",
                decision=decision,
                started=started,
            )

        # 2. Enforce agent's allowed tool list & read_only flag even if gate misconfigured
        if tool_name not in self.allowed_tools or (self.scope.read_only and spec.mutates):
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="not_allowed",
                error=f"Tool {tool_name} is not permitted for agent {self.agent_id} (read_only={self.scope.read_only})",
                decision=decision,
                started=started,
            )

        if decision.effect != "allow":
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="denied",
                error=f"PolicyGate denied {tool_name}: {decision.reason} ({decision.basis})",
                decision=decision,
                started=started,
            )

        # 3. Validate tool arguments using Pydantic model
        try:
            parsed_args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="invalid_arguments",
                error=f"Invalid arguments for {tool_name}: {errs}",
                decision=decision,
                started=started,
            )

        handler = self.registry.get_handler(tool_name)
        if handler is None:
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="not_registered",
                error=f"No handler registered for {tool_name}",
                decision=decision,
                started=started,
            )

        # 4. Execute handler with host-injected ScopeContext
        invocation = ToolInvocation(
            tool=spec,
            scope=self.scope,
            args=parsed_args,
            call_id=cid,
            decision=decision,
            session=self,
        )
        try:
            raw_out = handler(invocation)
        except ToolError as exc:
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind=exc.kind,
                error=exc.message,
                decision=decision,
                started=started,
            )
        except Exception as exc:
            return self._fail(
                seq=seq,
                call_id=cid,
                tool_name=tool_name,
                domain=domain,
                raw_args=raw_args,
                error_kind="failed",
                error=f"{type(exc).__name__}: {exc}",
                decision=decision,
                started=started,
            )

        out = raw_out if isinstance(raw_out, ToolOutput) else ToolOutput(data=dict(raw_out))
        call_citations = [
            Citation(call_id=cid, tool=tool_name, json_path=path, source=src) for path, src in out.sources
        ]
        self._citations.extend(call_citations)

        notes = list(out.notes)
        notes.append(f"scope_id={self.scope.scope_id}, as_of={self.scope.as_of}")
        payload = {"_notes": notes, **out.data}
        text = json.dumps(payload, ensure_ascii=False)
        elapsed = round(time.monotonic() - started, 4)

        record = ToolCallRecord(
            seq=seq,
            call_id=cid,
            tool=tool_name,
            domain=domain,
            arguments=raw_args,
            effect="allow",
            ok=True,
            result=out.data,
            duration_s=elapsed,
        )
        self._records.append(record)
        return ToolResponse(
            is_error=False,
            text=text,
            data=out.data,
            error_kind=None,
            error=None,
            citations=call_citations,
            decision=decision,
            record=record,
        )

    def _fail(
        self,
        *,
        seq: int,
        call_id: str,
        tool_name: str,
        domain: str,
        raw_args: dict[str, Any],
        error_kind: ToolErrorKind,
        error: str,
        decision: Decision | None,
        started: float,
    ) -> ToolResponse:
        effect: Literal["allow", "deny"] = "deny" if error_kind in _DENY_KINDS else "allow"
        hint = (
            "Tool query or action failed. Report the failure and error message accurately; "
            "do NOT fabricate data or assume the action succeeded."
        )
        payload = {
            "_notes": [hint],
            "tool": tool_name,
            "error_kind": error_kind,
            "error": error,
        }
        text = json.dumps(payload, ensure_ascii=False)
        elapsed = round(time.monotonic() - started, 4)
        record = ToolCallRecord(
            seq=seq,
            call_id=cid if (cid := call_id) else f"call_{seq}",
            tool=tool_name,
            domain=domain,
            arguments=raw_args,
            effect=effect,
            ok=False,
            result=None,
            error_kind=error_kind,
            error=error,
            duration_s=elapsed,
        )
        self._records.append(record)
        return ToolResponse(
            is_error=True,
            text=text,
            data=None,
            error_kind=error_kind,
            error=error,
            citations=[],
            decision=decision,
            record=record,
        )
