"""PolicyGate: Server-side RBAC, Agent-vs-Human Boundary Enforcement, and Autonomous Catalog Gate.

Every Host Tool call and every Human Action passes through `PolicyGate.authorize`:
1. Writes an immutable `AuditEntry` on every decision (both allow and deny).
2. Agents can NEVER execute human-only actions (`approve_intent`, `sign_rule_draft`, `publish_report`).
3. Humans can NEVER invoke Agent Host Tools directly.
4. Mutating Host Tools (`ticket`, `intent`, `report_draft`, `rule_draft`, `expert_memory`) follow explicit
   governance rules:
   - `report_draft` / `rule_draft` / `expert_memory`: allowed to create drafts or retain adjudicated memory;
     never directly activates rules or publishes reports.
   - `ticket`: checks catalog (`CatalogLookup`); network/infra categories require a valid catalog entry or
     fall back to human-triage referral.
   - `intent`: checks if covered by an active pre-approved catalog or autonomous mode (`autonomous_mode`);
     otherwise allowed as `draft_only` (creates a pending intent awaiting human approval).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from omp_framework.contracts import (
    Action,
    AuditEntry,
    CatalogEntry,
    Decision,
    GateContext,
    Principal,
)
from omp_framework.tools.registry import ToolRegistry

__all__ = [
    "AuditSink",
    "HumanActionSpec",
    "PolicyGate",
]


@dataclass(frozen=True, slots=True)
class HumanActionSpec:
    """Defines a human-only governance action and which roles are authorized to perform it."""

    name: str
    domain: str
    allowed_roles: tuple[str, ...]
    description: str = ""


class AuditSink:
    """In-memory and optional callback audit log sink."""

    def __init__(self, on_append: Callable[[AuditEntry], None] | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._on_append = on_append

    def append(self, entry: AuditEntry) -> AuditEntry:
        self._entries.append(entry)
        if self._on_append is not None:
            self._on_append(entry)
        return entry

    def list_entries(self, *, scope_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        items = self._entries
        if scope_id:
            items = [e for e in items if e.scope_id == scope_id]
        return list(reversed(items[-limit:]))


DEFAULT_HUMAN_ACTIONS: tuple[HumanActionSpec, ...] = (
    HumanActionSpec(
        name="approve_intent",
        domain="operations",
        allowed_roles=("ops_lead", "sre_approver", "admin"),
        description="Approve or reject a pending control-plane intent proposed by an agent",
    ),
    HumanActionSpec(
        name="sign_rule_draft",
        domain="policy",
        allowed_roles=("domain_expert", "safety_officer", "admin"),
        description="Sign and activate a simulated monitoring/governance rule draft",
    ),
    HumanActionSpec(
        name="publish_report",
        domain="reporting",
        allowed_roles=("domain_expert", "ops_lead", "admin"),
        description="Approve and publish a fact-checked agent report draft",
    ),
    HumanActionSpec(
        name="toggle_autonomous_mode",
        domain="operations",
        allowed_roles=("ops_lead", "admin"),
        description="Enable or disable bounded autonomous execution for cataloged intents",
    ),
)


class PolicyGate:
    """Central authorization gate for Agent Host Tools and Human Governance Actions."""

    def __init__(
        self,
        *,
        audit: AuditSink,
        tool_registry: ToolRegistry,
        agent_allowed_tools: Mapping[str, Sequence[str]] | None = None,
        human_actions: Sequence[HumanActionSpec] = DEFAULT_HUMAN_ACTIONS,
        catalog: Sequence[CatalogEntry] = (),
    ) -> None:
        self.audit = audit
        self.tool_registry = tool_registry
        self._agent_tools: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (agent_allowed_tools or {}).items()
        }
        self._human_actions: dict[str, HumanActionSpec] = {a.name: a for a in human_actions}
        self._catalog: dict[str, CatalogEntry] = {c.catalog_id: c for c in catalog}
        self.autonomous_enabled: bool = False

    def register_agent_tools(self, agent_id: str, tools: Sequence[str]) -> None:
        self._agent_tools[agent_id] = tuple(tools)

    def register_catalog(self, entry: CatalogEntry) -> None:
        self._catalog[entry.catalog_id] = entry

    def list_catalog(self) -> list[CatalogEntry]:
        return list(self._catalog.values())

    def match_catalog(self, action_kind: str, trigger_event: str | None = None) -> CatalogEntry | None:
        for entry in self._catalog.values():
            if entry.action_kind == action_kind:
                if not entry.allowed_triggers or (trigger_event and trigger_event in entry.allowed_triggers):
                    return entry
        return None

    def authorize(self, principal: Principal, action: Action, ctx: GateContext) -> Decision:
        decision = self._evaluate(principal, action, ctx)
        ts = (ctx.at or datetime.now(UTC)).isoformat()
        entry = AuditEntry(
            audit_id=f"AUD-{uuid.uuid4().hex[:10].upper()}",
            timestamp=ts,
            principal_kind=principal.kind,
            principal_id=principal.id,
            principal_role=principal.role,
            action_kind=action.kind,
            action_name=action.name,
            domain=decision.domain,
            scope_id=ctx.scope.scope_id,
            effect=decision.effect,
            basis=decision.basis,
            reason=decision.reason,
            catalog_ref=decision.catalog_ref,
            details={"read_only": ctx.scope.read_only},
        )
        self.audit.append(entry)
        return decision

    def _evaluate(self, principal: Principal, action: Action, ctx: GateContext) -> Decision:
        # Case 1: Human Action
        if action.kind == "human_action":
            spec = self._human_actions.get(action.name)
            if spec is None:
                return Decision(
                    effect="deny",
                    basis="default_deny",
                    domain=action.domain,
                    reason=f"Unknown human action: {action.name}",
                )
            if principal.kind == "agent":
                return Decision(
                    effect="deny",
                    basis="agent_human_action",
                    domain=spec.domain,
                    reason=f"Agent {principal.id} is strictly forbidden from executing human action {action.name}",
                    responsible_role=spec.allowed_roles[0] if spec.allowed_roles else None,
                )
            if principal.kind != "human" or principal.role not in spec.allowed_roles:
                return Decision(
                    effect="deny",
                    basis="action_identity",
                    domain=spec.domain,
                    reason=(
                        f"Principal {principal.id} (role={principal.role}) is not in allowed roles "
                        f"{spec.allowed_roles} for {action.name}"
                    ),
                    responsible_role=spec.allowed_roles[0] if spec.allowed_roles else None,
                )
            return Decision(
                effect="allow",
                basis="action_identity",
                domain=spec.domain,
                reason=f"Authorized human role {principal.role} for {action.name}",
                responsible_role=principal.role,
            )

        # Case 2: Host Tool Call
        if action.kind == "tool":
            tool_spec = self.tool_registry.get_spec(action.name)
            if tool_spec is None:
                return Decision(
                    effect="deny",
                    basis="default_deny",
                    domain=action.domain,
                    reason=f"Unknown Host Tool: {action.name}",
                )
            if principal.kind != "agent":
                return Decision(
                    effect="deny",
                    basis="default_deny",
                    domain=tool_spec.domain,
                    reason=f"Only agents may invoke Host Tools (got principal kind={principal.kind})",
                )
            allowed_for_agent = self._agent_tools.get(principal.id)
            if allowed_for_agent is not None and action.name not in allowed_for_agent:
                return Decision(
                    effect="deny",
                    basis="tool_not_allowed",
                    domain=tool_spec.domain,
                    reason=f"Tool {action.name} is not in agent {principal.id}'s declared tool list",
                )
            if ctx.scope.read_only and tool_spec.mutates:
                return Decision(
                    effect="deny",
                    basis="tool_not_allowed",
                    domain=tool_spec.domain,
                    reason=f"Mutating tool {action.name} denied in read_only scope",
                )

            rule = tool_spec.rule
            if rule == "read_only":
                return Decision(
                    effect="allow",
                    basis="read_only",
                    domain=tool_spec.domain,
                    reason=f"Read-only tool {action.name} allowed",
                )
            if rule == "report_draft":
                return Decision(
                    effect="allow",
                    basis="report_draft",
                    domain=tool_spec.domain,
                    reason="Report draft submission allowed (subject to deterministic fact-check)",
                )
            if rule == "rule_draft":
                return Decision(
                    effect="allow",
                    basis="rule_draft",
                    domain=tool_spec.domain,
                    reason="Rule draft proposal allowed (requires human expert signature to activate)",
                )
            if rule == "expert_memory":
                return Decision(
                    effect="allow",
                    basis="expert_memory",
                    domain=tool_spec.domain,
                    reason="Retaining human-validated expert criterion allowed",
                )
            if rule == "ticket":
                category = str(ctx.arguments.get("category", ""))
                trigger = str(ctx.arguments.get("event_id", ""))
                if category in ("referral", "human_triage"):
                    return Decision(
                        effect="allow",
                        basis="referral",
                        domain=tool_spec.domain,
                        reason=f"Ticket category {category} allowed as human referral",
                    )
                cat_entry = self.match_catalog(f"ticket:{category}")
                if cat_entry is not None:
                    return Decision(
                        effect="allow",
                        basis="catalog",
                        domain=tool_spec.domain,
                        reason=f"Ticket creation matched catalog {cat_entry.catalog_id} ({trigger})",
                        catalog_ref=cat_entry.catalog_id,
                    )
                return Decision(
                    effect="deny",
                    basis="catalog",
                    domain=tool_spec.domain,
                    reason=f"Ticket category {category!r} has no matching catalog entry",
                )
            if rule == "intent":
                kind = str(ctx.arguments.get("kind", ""))
                trigger = str(ctx.arguments.get("trigger_event", ""))
                cat_entry = self.match_catalog(f"intent:{kind}", trigger)
                if cat_entry is not None:
                    if self.autonomous_enabled and cat_entry.autonomous_allowed:
                        return Decision(
                            effect="allow",
                            basis="autonomous_mode",
                            domain=tool_spec.domain,
                            reason=f"Autonomous execution allowed under catalog {cat_entry.catalog_id}",
                            catalog_ref=cat_entry.catalog_id,
                        )
                    return Decision(
                        effect="allow",
                        basis="catalog",
                        domain=tool_spec.domain,
                        reason=f"Matched catalog {cat_entry.catalog_id}; queued for human approval",
                        catalog_ref=cat_entry.catalog_id,
                    )
                return Decision(
                    effect="allow",
                    basis="draft_only",
                    domain=tool_spec.domain,
                    reason=f"Uncataloged intent {kind!r} allowed as draft_only; requires human approval",
                )

        return Decision(
            effect="deny",
            basis="default_deny",
            domain=action.domain,
            reason="Default deny",
        )
