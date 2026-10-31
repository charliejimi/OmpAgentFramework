"""Event Trigger Bus: Ingests scheduled/external anomaly events, deduplicates them, and wakes up omp agents."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from omp_framework.contracts import AgentRunAnswer, ScopeContext, TriggerEventRecord

__all__ = ["EventTriggerBus"]


class EventTriggerBus:
    """Evaluates external/scheduled trigger events, deduplicates by `event_id`, and wakes a target agent."""

    def __init__(
        self,
        *,
        run_agent: Callable[[str, str, ScopeContext], AgentRunAnswer] | None = None,
    ) -> None:
        self._run_agent = run_agent
        self._events: list[TriggerEventRecord] = []
        self._seen_event_ids: set[tuple[str, str]] = set()

    def bind_runner(self, run_agent: Callable[[str, str, ScopeContext], AgentRunAnswer]) -> None:
        self._run_agent = run_agent

    def emit(
        self,
        *,
        event_id: str,
        scope_id: str,
        source: str,
        event_kind: str,
        target_agent: str = "ops",
        severity: Literal["info", "warning", "critical"] = "warning",
        payload: dict[str, Any] | None = None,
    ) -> TriggerEventRecord:
        dedup_key = (scope_id, event_id)
        now = datetime.now(UTC).isoformat()
        if dedup_key in self._seen_event_ids:
            record = TriggerEventRecord(
                event_id=event_id,
                occurred_at=now,
                scope_id=scope_id,
                source=source,
                event_kind=event_kind,
                target_Agent=target_agent,
                severity=severity,
                payload=dict(payload or {}),
                deduplicated=True,
                agent_invoked=False,
                agent_summary=f"Deduplicated: event {event_id} in scope {scope_id} was already processed.",
            )
            self._events.append(record)
            return record

        self._seen_event_ids.add(dedup_key)
        ticket_id: str | None = None
        intent_id: str | None = None
        summary = "Event recorded (no agent runner bound)"
        invoked = False

        if self._run_agent is not None:
            invoked = True
            prompt = (
                f"Automated Event Trigger `{event_id}` ({event_kind}, severity={severity}) from `{source}` "
                f"detected on scope `{scope_id}`. Payload: {payload or {}}. "
                f"Diagnose the issue, create a deduplicated ticket if needed, and propose a remediation intent."
            )
            ans = self._run_agent(target_agent, prompt, ScopeContext(scope_id=scope_id))
            summary = ans.answer
            for tc in ans.tool_calls:
                if tc.ok and isinstance(tc.result, dict):
                    if "ticket_id" in tc.result:
                        ticket_id = str(tc.result["ticket_id"])
                    if "intent_id" in tc.result:
                        intent_id = str(tc.result["intent_id"])

        record = TriggerEventRecord(
            event_id=event_id,
            occurred_at=now,
            scope_id=scope_id,
            source=source,
            event_kind=event_kind,
            target_Agent=target_agent,
            severity=severity,
            payload=dict(payload or {}),
            deduplicated=False,
            agent_invoked=invoked,
            ticket_id=ticket_id,
            intent_id=intent_id,
            agent_summary=summary,
        )
        self._events.append(record)
        return record

    def list_events(self, *, scope_id: str | None = None) -> list[TriggerEventRecord]:
        items = self._events
        if scope_id:
            items = [e for e in items if e.scope_id == scope_id]
        return list(reversed(items))
