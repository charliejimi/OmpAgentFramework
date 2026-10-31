"""Governance State Store for Intents, Tickets, Fact-Checked Report Drafts, and Simulated Rule Drafts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from omp_framework.contracts import (
    Action,
    GateContext,
    IntentItem,
    Principal,
    ReportDraft,
    RuleDraft,
    RuleSimulationResult,
    ScopeContext,
    StatementCheck,
    TicketItem,
)
from omp_framework.control.gate import PolicyGate

__all__ = ["GovernanceStore"]


class GovernanceStore:
    """Manages the lifecycle of human-in-the-loop proposals: Intents, Tickets, Reports, and Rules."""

    def __init__(self, gate: PolicyGate) -> None:
        self.gate = gate
        self._intents: dict[str, IntentItem] = {}
        self._tickets: dict[str, TicketItem] = {}
        self._reports: dict[str, ReportDraft] = {}
        self._rules: dict[str, RuleDraft] = {}
        self._active_rules: dict[str, dict[str, Any]] = {}

    # ── Control Plane Intents ──

    def propose_intent(
        self,
        *,
        scope_id: str,
        proposed_by: str,
        domain: str,
        kind: str,
        target: str,
        parameters: dict[str, Any],
        rationale: str,
        trigger_event: str,
        catalog_ref: str | None = None,
        autonomous_execute: bool = False,
    ) -> IntentItem:
        intent_id = f"INT-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(UTC).isoformat()
        status: Literal["pending_approval", "autonomous_executed"] = (
            "autonomous_executed" if autonomous_execute else "pending_approval"
        )
        item = IntentItem(
            intent_id=intent_id,
            created_at=now,
            scope_id=scope_id,
            proposed_by=proposed_by,
            domain=domain,
            kind=kind,
            target=target,
            parameters=parameters,
            rationale=rationale,
            trigger_event=trigger_event,
            catalog_ref=catalog_ref,
            status=status,
            reviewed_by="autonomous-catalog" if autonomous_execute else None,
            reviewed_at=now if autonomous_execute else None,
            review_note=f"Executed automatically under catalog {catalog_ref}" if autonomous_execute else None,
        )
        self._intents[intent_id] = item
        return item

    def review_intent(
        self,
        intent_id: str,
        *,
        principal: Principal,
        approve: bool,
        review_note: str = "",
    ) -> IntentItem:
        item = self._intents.get(intent_id)
        if item is None:
            raise KeyError(f"Intent not found: {intent_id}")
        decision = self.gate.authorize(
            principal,
            Action.human("approve_intent", domain="operations"),
            GateContext(scope=ScopeContext(scope_id=item.scope_id), arguments={"intent_id": intent_id, "approve": approve}),
        )
        if decision.effect != "allow":
            raise PermissionError(decision.reason)
        if item.status != "pending_approval":
            raise ValueError(f"Intent {intent_id} is already in status {item.status}")
        updated = item.model_copy(
            update={
                "status": "approved_executed" if approve else "rejected",
                "reviewed_by": f"{principal.id} ({principal.role})",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "review_note": review_note or ("Approved and executed" if approve else "Rejected by human reviewer"),
            }
        )
        self._intents[intent_id] = updated
        return updated

    def list_intents(self, *, scope_id: str | None = None) -> list[IntentItem]:
        items = list(self._intents.values())
        if scope_id:
            items = [i for i in items if i.scope_id == scope_id]
        return list(reversed(items))

    # ── Deduplicated Operational Tickets ──

    def create_ticket(
        self,
        *,
        scope_id: str,
        event_id: str,
        category: str,
        domain: str,
        severity: Literal["low", "medium", "high", "critical"],
        title: str,
        summary: str,
        created_by: str,
        catalog_ref: str | None = None,
    ) -> tuple[TicketItem, bool]:
        """Create an operational ticket, deduplicating by `(scope_id, event_id)` if an open ticket exists."""
        for existing in self._tickets.values():
            if existing.scope_id == scope_id and existing.event_id == event_id and existing.status == "open":
                return existing, True
        ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
        item = TicketItem(
            ticket_id=ticket_id,
            created_at=datetime.now(UTC).isoformat(),
            scope_id=scope_id,
            event_id=event_id,
            category=category,
            domain=domain,
            severity=severity,
            title=title,
            summary=summary,
            created_by=created_by,
            catalog_ref=catalog_ref,
            status="open",
        )
        self._tickets[ticket_id] = item
        return item, False

    def list_tickets(self, *, scope_id: str | None = None, status: str | None = None) -> list[TicketItem]:
        items = list(self._tickets.values())
        if scope_id:
            items = [t for t in items if t.scope_id == scope_id]
        if status:
            items = [t for t in items if t.status == status]
        return list(reversed(items))

    # ── Fact-Checked Report Drafts ──

    def save_report_draft(
        self,
        *,
        scope_id: str,
        agent_id: str,
        title: str,
        summary: str,
        metrics_snapshot: dict[str, Any],
        recommendations: list[str],
        checks: list[StatementCheck],
        rounds_used: int = 1,
    ) -> ReportDraft:
        passed = all(c.passed for c in checks)
        report_id = f"RPT-{uuid.uuid4().hex[:8].upper()}"
        draft = ReportDraft(
            report_id=report_id,
            created_at=datetime.now(UTC).isoformat(),
            scope_id=scope_id,
            agent_id=agent_id,
            title=title,
            summary=summary,
            metrics_snapshot=metrics_snapshot,
            recommendations=recommendations,
            checks=checks,
            passed_factcheck=passed,
            rounds_used=rounds_used,
            status="draft_verified" if passed else "draft_failed_check",
        )
        self._reports[report_id] = draft
        return draft

    def publish_report(self, report_id: str, *, principal: Principal) -> ReportDraft:
        draft = self._reports.get(report_id)
        if draft is None:
            raise KeyError(f"Report not found: {report_id}")
        decision = self.gate.authorize(
            principal,
            Action.human("publish_report", domain="reporting"),
            GateContext(scope=ScopeContext(scope_id=draft.scope_id), arguments={"report_id": report_id}),
        )
        if decision.effect != "allow":
            raise PermissionError(decision.reason)
        if not draft.passed_factcheck:
            raise ValueError(f"Report {report_id} cannot be published because fact-check did not pass")
        updated = draft.model_copy(
            update={"status": "published", "published_by": f"{principal.id} ({principal.role})"}
        )
        self._reports[report_id] = updated
        return updated

    def list_reports(self, *, scope_id: str | None = None) -> list[ReportDraft]:
        items = list(self._reports.values())
        if scope_id:
            items = [r for r in items if r.scope_id == scope_id]
        return list(reversed(items))

    # ── Versioned Rule / Workflow Drafts ──

    def get_active_rule(self, scope_id: str) -> dict[str, Any]:
        return self._active_rules.setdefault(
            scope_id,
            {
                "scope_id": scope_id,
                "version": 1,
                "thresholds": {"yellow": 0.70, "orange": 0.85, "red": 0.95},
                "safety_floor": {"yellow": 0.80, "orange": 0.90, "red": 0.98},
                "observation_window_minutes": 15,
                "notify_roles": ["oncall_sre", "ops_lead"],
            },
        )

    def propose_rule_draft(
        self,
        *,
        scope_id: str,
        proposed_by: str,
        candidate_thresholds: dict[str, float],
        observation_window_minutes: int,
        notify_roles: list[str],
        rationale: str,
        simulation: RuleSimulationResult,
    ) -> RuleDraft:
        active = self.get_active_rule(scope_id)
        cur = active["thresholds"]
        diffs = [candidate_thresholds.get(k, cur[k]) - cur[k] for k in cur]
        if all(d == 0 for d in diffs):
            direction: Literal["stricter", "looser", "mixed", "unchanged"] = "unchanged"
        elif all(d <= 0 for d in diffs):
            direction = "stricter"
        elif all(d >= 0 for d in diffs):
            direction = "looser"
        else:
            direction = "mixed"

        draft_id = f"RUL-{uuid.uuid4().hex[:8].upper()}"
        draft = RuleDraft(
            draft_id=draft_id,
            created_at=datetime.now(UTC).isoformat(),
            scope_id=scope_id,
            proposed_by=proposed_by,
            base_version=int(active["version"]),
            candidate_thresholds=candidate_thresholds,
            observation_window_minutes=observation_window_minutes,
            notify_roles=notify_roles,
            rationale=rationale,
            direction=direction,
            simulation=simulation,
            status="draft",
        )
        self._rules[draft_id] = draft
        return draft

    def sign_rule_draft(self, draft_id: str, *, principal: Principal) -> RuleDraft:
        draft = self._rules.get(draft_id)
        if draft is None:
            raise KeyError(f"Rule draft not found: {draft_id}")
        decision = self.gate.authorize(
            principal,
            Action.human("sign_rule_draft", domain="policy"),
            GateContext(scope=ScopeContext(scope_id=draft.scope_id), arguments={"draft_id": draft_id}),
        )
        if decision.effect != "allow":
            raise PermissionError(decision.reason)
        if draft.simulation and draft.simulation.violates_safety_floor:
            raise ValueError(f"Rule draft {draft_id} violates safety floor: {draft.simulation.floor_violation_reason}")

        active = self.get_active_rule(draft.scope_id)
        active["version"] = int(active["version"]) + 1
        active["thresholds"] = dict(draft.candidate_thresholds)
        active["observation_window_minutes"] = draft.observation_window_minutes
        active["notify_roles"] = list(draft.notify_roles)

        updated = draft.model_copy(
            update={"status": "signed_active", "signed_by": f"{principal.id} ({principal.role})"}
        )
        self._rules[draft_id] = updated
        return updated

    def list_rule_drafts(self, *, scope_id: str | None = None) -> list[RuleDraft]:
        items = list(self._rules.values())
        if scope_id:
            items = [r for r in items if r.scope_id == scope_id]
        return list(reversed(items))
