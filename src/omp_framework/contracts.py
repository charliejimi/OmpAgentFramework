"""Universal Domain-Agnostic Contracts for OmpAgentFramework.

Defines the core primitives shared across:
- Scope injection (`ScopeContext`) so LLMs never choose or cross-contaminate tenant/target/time scopes
- Provenance & Citations (`SourceRef`, `Citation`)
- Governance & RBAC (`Principal`, `Action`, `Decision`, `GateContext`, `AuditEntry`, `CatalogEntry`)
- Human-in-the-loop Proposals (`IntentItem`, `TicketItem`, `ReportDraft`, `RuleDraft`)
- Event Triggers (`TriggerEventRecord`) & Long-Term Expert Memory (`ExpertCriterion`)
- OpenAI-compatible LLM proxy models (`ChatCompletionRequest`, `ChatCompletionResponse`)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    """Strict base model for framework contracts (forbids unknown fields by default)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ── Execution Scope & Provenance ──────────────────────────────────────────────


class ScopeContext(ContractModel):
    """Host-injected execution scope.

    Unlike prompt text, `ScopeContext` is bound by the host before `omp` runs and is
    automatically injected into every Host Tool invocation. The LLM cannot override
    `scope_id`, `as_of`, or `tenant` in tool arguments.
    """

    scope_id: str = Field(min_length=1, description="Target resource/site/cluster/workspace identifier")
    as_of: str = Field(default="latest", description="Time cursor or snapshot date (ISO-8601 or 'latest')")
    tenant: str = Field(default="default", description="Tenant or organization identifier")
    read_only: bool = Field(default=False, description="When True, mutating tools are hidden and blocked by the Gate")
    tags: dict[str, str] = Field(default_factory=dict, description="Additional domain-specific scope metadata")


class SourceRef(ContractModel):
    """Provenance reference attached to read-only tool outputs for verifiable citations."""

    layer: str = Field(min_length=1, description="Data layer or subsystem (e.g. 'metrics', 'logs', 'topology', 'policy')")
    field: str = Field(min_length=1, description="Field or metric name")
    target: str | None = Field(default=None, description="Specific sub-entity or sensor/node ID")
    timestamp: str | None = Field(default=None, description="Observation timestamp or date")


class Citation(ContractModel):
    """Verifiable citation linking an agent's answer or report back to a specific Host Tool call."""

    call_id: str
    tool: str
    json_path: str
    source: SourceRef | None = None


# ── Principals, Actions, Policy Gate, & Audit ─────────────────────────────────

PrincipalKind = Literal["agent", "human", "system", "unknown"]
DecisionEffect = Literal["allow", "deny"]
GateBasis = Literal[
    "read_only",
    "action_identity",
    "agent_human_action",
    "tool_not_allowed",
    "catalog",
    "autonomous_mode",
    "draft_only",
    "report_draft",
    "rule_draft",
    "expert_memory",
    "referral",
    "default_deny",
]


@dataclass(frozen=True, slots=True)
class Principal:
    """Actor requesting an action or Host Tool execution."""

    kind: PrincipalKind
    id: str
    role: str | None = None
    domain: str | None = None

    @classmethod
    def for_agent(cls, agent_id: str, domain: str = "general") -> Principal:
        return cls(kind="agent", id=agent_id, role=None, domain=domain)

    @classmethod
    def for_human(cls, user_id: str, role: str, domain: str | None = None) -> Principal:
        return cls(kind="human", id=user_id, role=role, domain=domain)

    @classmethod
    def for_system(cls, component: str = "system") -> Principal:
        return cls(kind="system", id=component, role=None, domain="system")


@dataclass(frozen=True, slots=True)
class Action:
    """Action evaluated by the PolicyGate (either a Host Tool call or a Human Action)."""

    kind: Literal["tool", "human_action"]
    name: str
    domain: str = "general"

    @classmethod
    def tool(cls, name: str, domain: str = "general") -> Action:
        return cls(kind="tool", name=name, domain=domain)

    @classmethod
    def human(cls, name: str, domain: str = "general") -> Action:
        return cls(kind="human_action", name=name, domain=domain)


@dataclass(frozen=True, slots=True)
class GateContext:
    """Context passed into `PolicyGate.authorize`."""

    scope: ScopeContext
    arguments: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    at: dt.datetime | None = None


class Decision(ContractModel):
    """Authorization decision returned by `PolicyGate.authorize`."""

    effect: DecisionEffect
    basis: GateBasis
    domain: str
    reason: str
    responsible_role: str | None = None
    catalog_ref: str | None = None


class AuditEntry(ContractModel):
    """Immutable audit log entry recorded on every Gate check, LLM failure, or state transition."""

    audit_id: str
    timestamp: str
    principal_kind: PrincipalKind
    principal_id: str
    principal_role: str | None = None
    action_kind: str
    action_name: str
    domain: str
    scope_id: str
    effect: DecisionEffect
    basis: str
    reason: str
    catalog_ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CatalogEntry(ContractModel):
    """Pre-approved catalog item allowing bounded autonomous execution or fast-track approval."""

    catalog_id: str
    domain: str
    action_kind: str
    title: str
    description: str
    allowed_triggers: list[str] = Field(default_factory=list)
    autonomous_allowed: bool = False


# ── Proposals: Intents, Tickets, Report Drafts, & Rule Drafts ─────────────────

IntentStatus = Literal["pending_approval", "approved_executed", "autonomous_executed", "rejected"]


class IntentItem(ContractModel):
    """Control-plane action intent proposed by an agent, awaiting human approval or catalog execution."""

    intent_id: str
    created_at: str
    scope_id: str
    proposed_by: str
    domain: str
    kind: str
    target: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    trigger_event: str
    catalog_ref: str | None = None
    status: IntentStatus = "pending_approval"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None


class TicketItem(ContractModel):
    """Deduplicated operational ticket created by an agent through the Gate."""

    ticket_id: str
    created_at: str
    scope_id: str
    event_id: str
    category: str
    domain: str
    severity: Literal["low", "medium", "high", "critical"]
    title: str
    summary: str
    created_by: str
    catalog_ref: str | None = None
    status: Literal["open", "resolved"] = "open"


class StatementCheck(ContractModel):
    """Result of checking a single statement/claim in an agent's draft report against tool outputs."""

    statement_index: int
    claim: str
    passed: bool
    citation_path: str | None = None
    expected_value: Any = None
    actual_value: Any = None
    error: str | None = None


class ReportDraft(ContractModel):
    """Fact-checked report draft produced via the closed-loop verification tool."""

    report_id: str
    created_at: str
    scope_id: str
    agent_id: str
    title: str
    summary: str
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[str] = Field(default_factory=list)
    checks: list[StatementCheck] = Field(default_factory=list)
    passed_factcheck: bool = False
    rounds_used: int = 1
    status: Literal["draft_verified", "draft_failed_check", "published"] = "draft_verified"
    published_by: str | None = None


class RuleSimulationResult(ContractModel):
    """Backtest/simulation output comparing candidate rule thresholds against historical data."""

    scope_id: str
    evaluated_points: int
    current_alerts_triggered: int
    candidate_alerts_triggered: int
    delta_alerts: int
    violates_safety_floor: bool = False
    floor_violation_reason: str | None = None
    sample_matches: list[dict[str, Any]] = Field(default_factory=list)


class RuleDraft(ContractModel):
    """Versioned monitoring/governance rule draft compiled from natural language by a rulecard agent."""

    draft_id: str
    created_at: str
    scope_id: str
    proposed_by: str
    base_version: int
    candidate_thresholds: dict[str, float]
    observation_window_minutes: int
    notify_roles: list[str]
    rationale: str
    direction: Literal["stricter", "looser", "mixed", "unchanged"]
    simulation: RuleSimulationResult | None = None
    status: Literal["draft", "signed_active", "rejected"] = "draft"
    signed_by: str | None = None


# ── Long-Term Expert Memory & Event Triggers ──────────────────────────────────


class ExpertCriterion(ContractModel):
    """Human-validated decision criterion retained in long-term agent memory."""

    criterion_id: str
    created_at: str
    scope_id: str
    domain: str
    pattern_key: str
    verdict: Literal["normal", "watch", "escalate", "false_positive"]
    rationale: str
    recorded_by_agent: str
    approved_by_role: str


class TriggerEventRecord(ContractModel):
    """External/scheduled anomaly trigger event that wakes up a designated agent (with deduplication)."""

    event_id: str
    occurred_at: str
    scope_id: str
    source: str
    event_kind: str
    target_Agent: str = Field(default="ops")
    severity: Literal["info", "warning", "critical"] = "warning"
    payload: dict[str, Any] = Field(default_factory=dict)
    deduplicated: bool = False
    agent_invoked: bool = False
    ticket_id: str | None = None
    intent_id: str | None = None
    agent_summary: str | None = None


# ── Agent Execution & Tool Records ────────────────────────────────────────────


class ToolCallRecord(ContractModel):
    """Record of a single Host Tool call during an `omp` session."""

    seq: int
    call_id: str
    tool: str
    domain: str
    arguments: dict[str, Any]
    effect: DecisionEffect
    ok: bool
    result: dict[str, Any] | None = None
    error_kind: str | None = None
    error: str | None = None
    duration_s: float = 0.0


class AgentRunAnswer(ContractModel):
    """Structured response returned by `AgentRunner.run`."""

    run_id: str
    agent_id: str
    domain: str
    scope_id: str
    as_of: str
    mode: str
    status: Literal["ok", "aborted", "refused", "error"]
    answer: str
    session_id: str | None = None
    session_file: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    abort_reason: str | None = None
    duration_s: float = 0.0


# ── OpenAI-Compatible `/llm/v1` Models ────────────────────────────────────────


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def text_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts: list[str] = []
            for item in self.content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "\n".join(parts)
        return ""


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def tool_names(self) -> tuple[str, ...]:
        if not self.tools:
            return ()
        names: list[str] = []
        for t in self.tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                names.append(fn["name"])
        return tuple(names)


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCallSpec(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCallSpec] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None
    usage: Usage = Field(default_factory=Usage)
