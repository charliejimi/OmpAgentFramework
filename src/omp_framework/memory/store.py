"""Long-Term Expert Memory Store: Retains and recalls human-validated decision criteria across runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from omp_framework.contracts import ExpertCriterion

__all__ = ["ExpertMemoryStore"]


class ExpertMemoryStore:
    """Stores human-adjudicated decision criteria so agents can recall precedent on similar anomalies."""

    def __init__(self, initial_criteria: list[ExpertCriterion] | None = None) -> None:
        self._items: dict[str, ExpertCriterion] = {}
        for item in initial_criteria or []:
            self._items[item.pattern_key] = item

    def retain(
        self,
        *,
        scope_id: str,
        domain: str,
        pattern_key: str,
        verdict: Literal["normal", "watch", "escalate", "false_positive"],
        rationale: str,
        recorded_by_agent: str,
        approved_by_role: str = "domain_expert",
    ) -> ExpertCriterion:
        item = ExpertCriterion(
            criterion_id=f"MEM-{uuid.uuid4().hex[:8].upper()}",
            created_at=datetime.now(UTC).isoformat(),
            scope_id=scope_id,
            domain=domain,
            pattern_key=pattern_key,
            verdict=verdict,
            rationale=rationale,
            recorded_by_agent=recorded_by_agent,
            approved_by_role=approved_by_role,
        )
        self._items[pattern_key] = item
        return item

    def recall(self, *, domain: str | None = None, query: str | None = None) -> list[ExpertCriterion]:
        items = list(self._items.values())
        if domain:
            items = [i for i in items if i.domain == domain]
        if query:
            q = query.lower()
            items = [
                i
                for i in items
                if q in i.pattern_key.lower() or q in i.rationale.lower() or q in i.verdict.lower()
            ]
        return items
