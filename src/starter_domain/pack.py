"""Starter Domain Pack implementation with 4 universal agent archetypes and 9 Host Tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from omp_framework.contracts import (
    CatalogEntry,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ContractModel,
    ExpertCriterion,
    RuleSimulationResult,
    SourceRef,
    StatementCheck,
)
from omp_framework.control.gate import PolicyGate
from omp_framework.control.store import GovernanceStore
from omp_framework.llm.backend import (
    OfflinePlanner,
    make_text_response,
    make_tool_call_response,
)
from omp_framework.memory.store import ExpertMemoryStore
from omp_framework.tools.registry import (
    NoArgs,
    ToolError,
    ToolInvocation,
    ToolOutput,
    ToolRegistry,
)

# ── Sample Multi-Scope Domain Data ────────────────────────────────────────────

SAMPLE_SCOPES: dict[str, dict[str, Any]] = {
    "prod-cluster-01": {
        "scope_id": "prod-cluster-01",
        "name": "Production Kubernetes Cluster 01 (AP-East)",
        "as_of": "2026-10-31",
        "overall_grade": 2,
        "overall_status": "degraded_orange",
        "availability_sla": 0.9942,
        "metrics": {
            "cpu_load_ratio": 0.88,
            "memory_pressure_ratio": 0.76,
            "error_rate_5xx": 0.034,
            "p99_latency_ms": 420.0,
        },
        "nodes": [
            {"node_id": "node-api-01", "load_ratio": 0.91, "light": "orange", "link_status": "interrupted", "sensor_trust": "link_freeze"},
            {"node_id": "node-api-02", "load_ratio": 0.84, "light": "yellow", "link_status": "ok", "sensor_trust": "trusted"},
            {"node_id": "node-db-01", "load_ratio": 0.62, "light": "green", "link_status": "ok", "sensor_trust": "trusted"},
        ],
        "recent_anomalies": [
            {
                "event_id": "EVT-20261031-01",
                "node_id": "node-api-01",
                "kind": "link_packet_loss",
                "detail": "Upstream gateway telemetry lost for 15m; cross-ID classifies reading freeze as link_freeze.",
            },
            {
                "event_id": "EVT-20261031-02",
                "node_id": "node-api-02",
                "kind": "latency_spike",
                "detail": "P99 latency rose to 420ms following traffic surge.",
            },
        ],
        "historical_ratios": [0.55, 0.62, 0.68, 0.72, 0.79, 0.84, 0.88, 0.91, 0.94, 0.74],
    },
    "edge-site-alpha": {
        "scope_id": "edge-site-alpha",
        "name": "Smart Factory Edge Site Alpha",
        "as_of": "2026-10-31",
        "overall_grade": 0,
        "overall_status": "healthy_green",
        "availability_sla": 0.9995,
        "metrics": {
            "cpu_load_ratio": 0.45,
            "memory_pressure_ratio": 0.50,
            "error_rate_5xx": 0.001,
            "p99_latency_ms": 28.0,
        },
        "nodes": [
            {"node_id": "plc-gateway-01", "load_ratio": 0.48, "light": "green", "link_status": "ok", "sensor_trust": "trusted"},
            {"node_id": "plc-gateway-02", "load_ratio": 0.42, "light": "green", "link_status": "ok", "sensor_trust": "trusted"},
        ],
        "recent_anomalies": [],
        "historical_ratios": [0.38, 0.40, 0.42, 0.45, 0.47, 0.49, 0.44, 0.41, 0.43, 0.45],
    },
}

DEFAULT_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        catalog_id="CAT-NET-REROUTE-01",
        domain="operations",
        action_kind="intent:reroute_traffic",
        title="Shift traffic away from degraded gateway node",
        description="Pre-approved intent to drain and reroute traffic when link_packet_loss or latency_spike occurs.",
        allowed_triggers=["EVT-20261031-01", "EVT-20261031-02", "link_packet_loss", "latency_spike"],
        autonomous_allowed=True,
    ),
    CatalogEntry(
        catalog_id="CAT-TKT-NETWORK-01",
        domain="operations",
        action_kind="ticket:network",
        title="Open network link investigation ticket",
        description="Standard ticket catalog for telemetry link interruptions.",
        allowed_triggers=[],
        autonomous_allowed=True,
    ),
    CatalogEntry(
        catalog_id="CAT-TKT-COMPUTE-01",
        domain="operations",
        action_kind="ticket:compute",
        title="Open compute capacity investigation ticket",
        description="Standard ticket catalog for CPU/latency threshold crossings.",
        allowed_triggers=[],
        autonomous_allowed=True,
    ),
)

DEFAULT_EXPERT_CRITERIA: list[ExpertCriterion] = [
    ExpertCriterion(
        criterion_id="MEM-SEED-001",
        created_at="2026-10-15T08:00:00+00:00",
        scope_id="prod-cluster-01",
        domain="operations",
        pattern_key="link_freeze_with_upstream_packet_loss",
        verdict="watch",
        rationale="When sensor reading freezes simultaneously with upstream gateway packet loss, classify as telemetry link freeze rather than hardware sensor failure; open network ticket and reroute traffic.",
        recorded_by_agent="ops",
        approved_by_role="domain_expert",
    )
]


# ── Tool Argument Models (Pydantic -> JSON Schema for omp) ────────────────────


class SubmitReportDraftArgs(ContractModel):
    title: str = Field(min_length=3, description="Report title")
    summary: str = Field(min_length=10, description="Executive summary citing verified metrics")
    claimed_cpu_load_ratio: float = Field(description="Must match get_system_metrics.metrics.cpu_load_ratio")
    claimed_p99_latency_ms: float = Field(description="Must match get_system_metrics.metrics.p99_latency_ms")
    claimed_availability_sla: float = Field(description="Must match get_system_metrics.availability_sla")
    recommendations: list[str] = Field(min_length=1, description="Actionable engineering recommendations")


class SimulateRuleArgs(ContractModel):
    yellow_threshold: float = Field(gt=0.0, lt=1.0, description="Candidate yellow alert threshold (0..1)")
    orange_threshold: float = Field(gt=0.0, lt=1.0, description="Candidate orange alert threshold (0..1)")
    red_threshold: float = Field(gt=0.0, lt=1.0, description="Candidate red alert threshold (0..1)")
    observation_window_minutes: int = Field(ge=5, le=120, default=15, description="Observation window in minutes")


class ProposeRuleDraftArgs(ContractModel):
    yellow_threshold: float = Field(gt=0.0, lt=1.0)
    orange_threshold: float = Field(gt=0.0, lt=1.0)
    red_threshold: float = Field(gt=0.0, lt=1.0)
    observation_window_minutes: int = Field(ge=5, le=120, default=15)
    notify_roles: list[str] = Field(default_factory=lambda: ["oncall_sre", "ops_lead"])
    rationale: str = Field(min_length=5, description="Why this threshold change is proposed")


class CreateTicketArgs(ContractModel):
    event_id: str = Field(min_length=3, description="Triggering anomaly event ID for deduplication")
    category: Literal["network", "compute", "referral"] = Field(description="Ticket category")
    severity: Literal["low", "medium", "high", "critical"] = Field(default="high")
    title: str = Field(min_length=3)
    summary: str = Field(min_length=5)


class ProposeIntentArgs(ContractModel):
    kind: Literal["reroute_traffic", "scale_cluster", "restart_gateway"] = Field(description="Control intent kind")
    target: str = Field(min_length=2, description="Target node or service ID")
    trigger_event: str = Field(min_length=2, description="Triggering event ID or anomaly kind")
    rationale: str = Field(min_length=5, description="Engineering rationale for the intent")
    parameters: dict[str, Any] = Field(default_factory=dict)


class RetainCriterionArgs(ContractModel):
    pattern_key: str = Field(min_length=3, description="Unique anomaly pattern key")
    verdict: Literal["normal", "watch", "escalate", "false_positive"]
    rationale: str = Field(min_length=5, description="Expert decision rationale to retain in long-term memory")


# ── Starter Domain Pack Container ─────────────────────────────────────────────


class StarterDomainPack:
    """Self-contained domain pack wiring data, Host Tools, governance store, memory, and offline planners."""

    name: str = "service-reliability-sentinel"

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        gate: PolicyGate,
        store: GovernanceStore,
        memory: ExpertMemoryStore,
        planner: OfflinePlanner,
    ) -> None:
        self.registry = registry
        self.gate = gate
        self.store = store
        self.memory = memory
        self.planner = planner
        self.scopes: dict[str, dict[str, Any]] = {k: dict(v) for k, v in SAMPLE_SCOPES.items()}
        self._register_catalog()
        self._register_tools()
        self._register_offline_planners()

    def get_scope_data(self, scope_id: str) -> dict[str, Any]:
        return self.scopes.get(scope_id) or self.scopes["prod-cluster-01"]

    def _register_catalog(self) -> None:
        for entry in DEFAULT_CATALOG:
            self.gate.register_catalog(entry)

    def _simulate_thresholds(
        self,
        scope_id: str,
        yellow: float,
        orange: float,
        red: float,
    ) -> RuleSimulationResult:
        data = self.get_scope_data(scope_id)
        active = self.store.get_active_rule(scope_id)
        cur_y = float(active["thresholds"]["yellow"])
        floor = active["safety_floor"]
        ratios: list[float] = list(data["historical_ratios"])

        cur_alerts = sum(1 for r in ratios if r >= cur_y)
        cand_alerts = sum(1 for r in ratios if r >= yellow)

        violates = False
        reason = None
        if not (yellow < orange < red):
            violates = True
            reason = f"Thresholds must satisfy yellow ({yellow}) < orange ({orange}) < red ({red})"
        elif yellow > floor["yellow"] or orange > floor["orange"] or red > floor["red"]:
            violates = True
            reason = (
                f"Candidate thresholds ({yellow}/{orange}/{red}) are looser than safety floor "
                f"({floor['yellow']}/{floor['orange']}/{floor['red']})"
            )

        return RuleSimulationResult(
            scope_id=scope_id,
            evaluated_points=len(ratios),
            current_alerts_triggered=cur_alerts,
            candidate_alerts_triggered=cand_alerts,
            delta_alerts=cand_alerts - cur_alerts,
            violates_safety_floor=violates,
            floor_violation_reason=reason,
            sample_matches=[
                {"sample_idx": idx, "ratio": r, "cand_alert": r >= yellow} for idx, r in enumerate(ratios[:5])
            ],
        )

    def _register_tools(self) -> None:
        @self.registry.tool(
            "get_system_metrics",
            label="Get System Metrics & SLA",
            description="Read current health grade, SLA availability, CPU/memory/latency metrics, and node light statuses for the active scope.",
            domain="monitoring",
            rule="read_only",
            args_model=NoArgs,
        )
        def get_system_metrics(inv: ToolInvocation) -> ToolOutput:
            inv.report_progress(f"Reading telemetry & SLA for scope {inv.scope.scope_id}...")
            d = self.get_scope_data(inv.scope.scope_id)
            return ToolOutput(
                data={
                    "scope_id": d["scope_id"],
                    "name": d["name"],
                    "as_of": d["as_of"],
                    "overall_grade": d["overall_grade"],
                    "overall_status": d["overall_status"],
                    "availability_sla": d["availability_sla"],
                    "metrics": d["metrics"],
                    "nodes": d["nodes"],
                },
                sources=[
                    ("metrics.cpu_load_ratio", SourceRef(layer="telemetry.metrics", field="cpu_load_ratio", target=d["scope_id"], timestamp=d["as_of"])),
                    ("metrics.p99_latency_ms", SourceRef(layer="telemetry.metrics", field="p99_latency_ms", target=d["scope_id"], timestamp=d["as_of"])),
                    ("availability_sla", SourceRef(layer="outputs.sla", field="availability_sla", target=d["scope_id"], timestamp=d["as_of"])),
                ],
            )

        @self.registry.tool(
            "list_recent_anomalies",
            label="List Recent Anomalies & Cross-ID",
            description="List recent anomaly events and cross-identification evidence (sensor vs. telemetry link freeze) for the active scope.",
            domain="monitoring",
            rule="read_only",
            args_model=NoArgs,
        )
        def list_recent_anomalies(inv: ToolInvocation) -> ToolOutput:
            d = self.get_scope_data(inv.scope.scope_id)
            return ToolOutput(
                data={
                    "scope_id": d["scope_id"],
                    "anomalies": d["recent_anomalies"],
                    "open_tickets": [t.model_dump() for t in self.store.list_tickets(scope_id=d["scope_id"], status="open")],
                },
                sources=[
                    ("anomalies", SourceRef(layer="trust.cross_id", field="recent_anomalies", target=d["scope_id"], timestamp=d["as_of"])),
                ],
            )

        @self.registry.tool(
            "submit_report_draft",
            label="Submit & Fact-Check Report Draft",
            description="Submit a structured diagnostic report draft. The host deterministically verifies claimed metrics against `get_system_metrics` output in this session.",
            domain="reporting",
            rule="report_draft",
            args_model=SubmitReportDraftArgs,
        )
        def submit_report_draft(inv: ToolInvocation) -> ToolOutput:
            args: SubmitReportDraftArgs = inv.args
            metrics_call = next(
                (r for r in reversed(inv.session.records) if r.tool == "get_system_metrics" and r.ok and r.result),
                None,
            )
            if metrics_call is None or metrics_call.result is None:
                raise ToolError("You must call `get_system_metrics` before `submit_report_draft` so claims can be fact-checked.")

            actual_metrics = metrics_call.result.get("metrics", {})
            actual_cpu = float(actual_metrics.get("cpu_load_ratio", 0.0))
            actual_lat = float(actual_metrics.get("p99_latency_ms", 0.0))
            actual_sla = float(metrics_call.result.get("availability_sla", 0.0))

            checks = [
                StatementCheck(
                    statement_index=1,
                    claim="cpu_load_ratio",
                    passed=abs(args.claimed_cpu_load_ratio - actual_cpu) < 1e-4,
                    citation_path="metrics.cpu_load_ratio",
                    expected_value=actual_cpu,
                    actual_value=args.claimed_cpu_load_ratio,
                    error=None if abs(args.claimed_cpu_load_ratio - actual_cpu) < 1e-4 else f"Expected {actual_cpu}, got {args.claimed_cpu_load_ratio}",
                ),
                StatementCheck(
                    statement_index=2,
                    claim="p99_latency_ms",
                    passed=abs(args.claimed_p99_latency_ms - actual_lat) < 1e-2,
                    citation_path="metrics.p99_latency_ms",
                    expected_value=actual_lat,
                    actual_value=args.claimed_p99_latency_ms,
                    error=None if abs(args.claimed_p99_latency_ms - actual_lat) < 1e-2 else f"Expected {actual_lat}, got {args.claimed_p99_latency_ms}",
                ),
                StatementCheck(
                    statement_index=3,
                    claim="availability_sla",
                    passed=abs(args.claimed_availability_sla - actual_sla) < 1e-5,
                    citation_path="availability_sla",
                    expected_value=actual_sla,
                    actual_value=args.claimed_availability_sla,
                    error=None if abs(args.claimed_availability_sla - actual_sla) < 1e-5 else f"Expected {actual_sla}, got {args.claimed_availability_sla}",
                ),
            ]

            rounds = int(inv.session.state.get("factcheck_rounds", 0)) + 1
            inv.session.state["factcheck_rounds"] = rounds

            draft = self.store.save_report_draft(
                scope_id=inv.scope.scope_id,
                agent_id=inv.session.agent_id,
                title=args.title,
                summary=args.summary,
                metrics_snapshot={"cpu_load_ratio": actual_cpu, "p99_latency_ms": actual_lat, "availability_sla": actual_sla},
                recommendations=args.recommendations,
                checks=checks,
                rounds_used=rounds,
            )
            return ToolOutput(
                data=draft.model_dump(),
                sources=[("report_id", SourceRef(layer="outputs.report", field="report_id", target=draft.report_id))],
            )

        @self.registry.tool(
            "get_active_rule",
            label="Get Active Monitoring Rule Card",
            description="Read the currently active threshold rule card and safety floor for the scope.",
            domain="policy",
            rule="read_only",
            args_model=NoArgs,
        )
        def get_active_rule(inv: ToolInvocation) -> ToolOutput:
            rule = self.store.get_active_rule(inv.scope.scope_id)
            return ToolOutput(
                data=rule,
                sources=[("version", SourceRef(layer="policy.rulecard", field="version", target=inv.scope.scope_id))],
            )

        @self.registry.tool(
            "simulate_rule_change",
            label="Simulate Rule Thresholds on Historical Data",
            description="Backtest candidate yellow/orange/red thresholds against historical series and check safety floor compliance.",
            domain="policy",
            rule="read_only",
            args_model=SimulateRuleArgs,
        )
        def simulate_rule_change(inv: ToolInvocation) -> ToolOutput:
            args: SimulateRuleArgs = inv.args
            sim = self._simulate_thresholds(
                inv.scope.scope_id,
                args.yellow_threshold,
                args.orange_threshold,
                args.red_threshold,
            )
            inv.session.state["last_simulation"] = sim
            return ToolOutput(
                data=sim.model_dump(),
                sources=[("delta_alerts", SourceRef(layer="policy.simulation", field="delta_alerts", target=inv.scope.scope_id))],
            )

        @self.registry.tool(
            "propose_rule_draft",
            label="Propose Versioned Rule Draft",
            description="Create a versioned rule card draft (never auto-activates; requires human expert signature).",
            domain="policy",
            rule="rule_draft",
            args_model=ProposeRuleDraftArgs,
        )
        def propose_rule_draft(inv: ToolInvocation) -> ToolOutput:
            args: ProposeRuleDraftArgs = inv.args
            sim = self._simulate_thresholds(
                inv.scope.scope_id,
                args.yellow_threshold,
                args.orange_threshold,
                args.red_threshold,
            )
            if sim.violates_safety_floor:
                raise ToolError(f"Cannot propose rule draft violating safety floor: {sim.floor_violation_reason}")
            draft = self.store.propose_rule_draft(
                scope_id=inv.scope.scope_id,
                proposed_by=inv.session.agent_id,
                candidate_thresholds={
                    "yellow": args.yellow_threshold,
                    "orange": args.orange_threshold,
                    "red": args.red_threshold,
                },
                observation_window_minutes=args.observation_window_minutes,
                notify_roles=args.notify_roles,
                rationale=args.rationale,
                simulation=sim,
            )
            return ToolOutput(
                data=draft.model_dump(),
                sources=[("draft_id", SourceRef(layer="policy.draft", field="draft_id", target=draft.draft_id))],
            )

        @self.registry.tool(
            "recall_expert_criteria",
            label="Recall Long-Term Expert Criteria",
            description="Search long-term memory for past human-adjudicated anomaly patterns and decision criteria.",
            domain="operations",
            rule="read_only",
            args_model=NoArgs,
        )
        def recall_expert_criteria(inv: ToolInvocation) -> ToolOutput:
            items = [c.model_dump() for c in self.memory.recall()]
            return ToolOutput(
                data={"criteria": items, "count": len(items)},
                sources=[("criteria", SourceRef(layer="memory.expert_criteria", field="criteria", target=inv.scope.scope_id))],
            )

        @self.registry.tool(
            "create_ticket",
            label="Create Deduplicated Incident Ticket",
            description="Create an operational ticket for an anomaly event (deduplicated by scope_id and event_id).",
            domain="operations",
            rule="ticket",
            args_model=CreateTicketArgs,
        )
        def create_ticket(inv: ToolInvocation) -> ToolOutput:
            args: CreateTicketArgs = inv.args
            ticket, deduped = self.store.create_ticket(
                scope_id=inv.scope.scope_id,
                event_id=args.event_id,
                category=args.category,
                domain="operations",
                severity=args.severity,
                title=args.title,
                summary=args.summary,
                created_by=inv.session.agent_id,
                catalog_ref=inv.decision.catalog_ref,
            )
            return ToolOutput(
                data={**ticket.model_dump(), "deduplicated": deduped},
                sources=[("ticket_id", SourceRef(layer="control.tickets", field="ticket_id", target=ticket.ticket_id))],
            )

        @self.registry.tool(
            "propose_intent",
            label="Propose Control-Plane Action Intent",
            description="Propose a remediation control intent. Enters pending human approval unless autonomous catalog mode is enabled.",
            domain="operations",
            rule="intent",
            args_model=ProposeIntentArgs,
        )
        def propose_intent(inv: ToolInvocation) -> ToolOutput:
            args: ProposeIntentArgs = inv.args
            auto_exec = inv.decision.basis == "autonomous_mode"
            intent = self.store.propose_intent(
                scope_id=inv.scope.scope_id,
                proposed_by=inv.session.agent_id,
                domain="operations",
                kind=args.kind,
                target=args.target,
                parameters=args.parameters,
                rationale=args.rationale,
                trigger_event=args.trigger_event,
                catalog_ref=inv.decision.catalog_ref,
                autonomous_execute=auto_exec,
            )
            return ToolOutput(
                data=intent.model_dump(),
                sources=[("intent_id", SourceRef(layer="control.intents", field="intent_id", target=intent.intent_id))],
            )

        @self.registry.tool(
            "retain_expert_criterion",
            label="Retain Human-Adjudicated Criterion in Memory",
            description="Save a verified anomaly decision rule into long-term expert memory.",
            domain="operations",
            rule="expert_memory",
            args_model=RetainCriterionArgs,
        )
        def retain_expert_criterion(inv: ToolInvocation) -> ToolOutput:
            args: RetainCriterionArgs = inv.args
            crit = self.memory.retain(
                scope_id=inv.scope.scope_id,
                domain="operations",
                pattern_key=args.pattern_key,
                verdict=args.verdict,
                rationale=args.rationale,
                recorded_by_agent=inv.session.agent_id,
            )
            return ToolOutput(
                data=crit.model_dump(),
                sources=[("criterion_id", SourceRef(layer="memory.expert_criteria", field="criterion_id", target=crit.criterion_id))],
            )

    def _register_offline_planners(self) -> None:
        """Register deterministic multi-turn planners for all 4 starter agent archetypes."""

        # 1. Query Agent: calls get_system_metrics -> list_recent_anomalies -> answers with citations
        def plan_query(req: ChatCompletionRequest, history: list[tuple[str, dict[str, Any]]]) -> ChatCompletionResponse:
            called = {name for name, _ in history}
            if "get_system_metrics" not in called:
                return make_tool_call_response("get_system_metrics", {}, call_id="call_q_1")
            if "list_recent_anomalies" not in called:
                return make_tool_call_response("list_recent_anomalies", {}, call_id="call_q_2")
            m_data = next(res for name, res in history if name == "get_system_metrics")
            a_data = next(res for name, res in history if name == "list_recent_anomalies")
            metrics = m_data.get("metrics", {})
            anomalies = a_data.get("anomalies", [])
            return make_text_response(
                f"### Scope Diagnostic Summary (`{m_data.get('scope_id')}` as of `{m_data.get('as_of')}`)\n"
                f"- **Overall Status**: `{m_data.get('overall_status')}` (Grade {m_data.get('overall_grade')})\n"
                f"- **Availability SLA**: `{m_data.get('availability_sla')}`\n"
                f"- **Metrics**: CPU Load Ratio = `{metrics.get('cpu_load_ratio')}`, "
                f"P99 Latency = `{metrics.get('p99_latency_ms')} ms`, 5xx Error Rate = `{metrics.get('error_rate_5xx')}`\n"
                f"- **Active Anomalies ({len(anomalies)})**: "
                + (
                    "; ".join(f"`{a['event_id']}` on `{a['node_id']}` ({a['kind']}): {a['detail']}" for a in anomalies)
                    if anomalies
                    else "None."
                )
            )

        # 2. Report Agent: calls get_system_metrics -> submit_report_draft -> returns verified report summary
        def plan_report(req: ChatCompletionRequest, history: list[tuple[str, dict[str, Any]]]) -> ChatCompletionResponse:
            called = {name for name, _ in history}
            if "get_system_metrics" not in called:
                return make_tool_call_response("get_system_metrics", {}, call_id="call_r_1")
            m_data = next(res for name, res in history if name == "get_system_metrics")
            metrics = m_data.get("metrics", {})
            if "submit_report_draft" not in called:
                return make_tool_call_response(
                    "submit_report_draft",
                    {
                        "title": f"Operational Health & SLA Assessment — {m_data.get('scope_id')}",
                        "summary": (
                            f"Scope {m_data.get('scope_id')} is at status {m_data.get('overall_status')} with "
                            f"CPU load ratio {metrics.get('cpu_load_ratio')}, P99 latency {metrics.get('p99_latency_ms')} ms, "
                            f"and availability SLA {m_data.get('availability_sla')}."
                        ),
                        "claimed_cpu_load_ratio": float(metrics.get("cpu_load_ratio", 0.0)),
                        "claimed_p99_latency_ms": float(metrics.get("p99_latency_ms", 0.0)),
                        "claimed_availability_sla": float(m_data.get("availability_sla", 0.0)),
                        "recommendations": [
                            "Investigate upstream packet loss on node-api-01 (classified as link_freeze).",
                            "Review traffic rerouting intent to relieve P99 latency on node-api-02.",
                        ],
                    },
                    call_id="call_r_2",
                )
            r_data = next(res for name, res in history if name == "submit_report_draft")
            return make_text_response(
                f"### Fact-Checked Report Draft Created (`{r_data.get('report_id')}`)\n"
                f"- **Fact-Check Status**: `passed={r_data.get('passed_factcheck')}` (Rounds used: {r_data.get('rounds_used')})\n"
                f"- **Verified Metrics**: CPU Load `{metrics.get('cpu_load_ratio')}`, "
                f"P99 Latency `{metrics.get('p99_latency_ms')} ms`, SLA `{m_data.get('availability_sla')}`\n"
                f"- **Next Step**: Ready for human approval (`publish_report`)."
            )

        # 3. Rulecard Agent: calls get_active_rule -> simulate_rule_change -> propose_rule_draft
        def plan_rulecard(req: ChatCompletionRequest, history: list[tuple[str, dict[str, Any]]]) -> ChatCompletionResponse:
            called = {name for name, _ in history}
            if "get_active_rule" not in called:
                return make_tool_call_response("get_active_rule", {}, call_id="call_rc_1")
            if "simulate_rule_change" not in called:
                return make_tool_call_response(
                    "simulate_rule_change",
                    {
                        "yellow_threshold": 0.65,
                        "orange_threshold": 0.80,
                        "red_threshold": 0.92,
                        "observation_window_minutes": 15,
                    },
                    call_id="call_rc_2",
                )
            if "propose_rule_draft" not in called:
                return make_tool_call_response(
                    "propose_rule_draft",
                    {
                        "yellow_threshold": 0.65,
                        "orange_threshold": 0.80,
                        "red_threshold": 0.92,
                        "observation_window_minutes": 15,
                        "notify_roles": ["oncall_sre", "ops_lead"],
                        "rationale": "Tighten yellow/orange/red thresholds to catch latency spikes earlier while staying within safety floor.",
                    },
                    call_id="call_rc_3",
                )
            sim_data = next(res for name, res in history if name == "simulate_rule_change")
            draft_data = next(res for name, res in history if name == "propose_rule_draft")
            return make_text_response(
                f"### Rule Card Draft Proposed (`{draft_data.get('draft_id')}`)\n"
                f"- **Direction**: `{draft_data.get('direction')}` (Base Version v{draft_data.get('base_version')})\n"
                f"- **Candidate Thresholds**: Yellow `0.65`, Orange `0.80`, Red `0.92`\n"
                f"- **Historical Backtest**: `{sim_data.get('current_alerts_triggered')}` -> "
                f"`{sim_data.get('candidate_alerts_triggered')}` alerts (Delta `{sim_data.get('delta_alerts'):+d}`, "
                f"Safety Floor Violation: `{sim_data.get('violates_safety_floor')}`)\n"
                f"- **Next Step**: Awaiting human expert signature (`sign_rule_draft`)."
            )

        # 4. Ops & Intent Agent: recalls expert memory -> inspects anomalies -> creates ticket -> proposes intent
        def plan_ops(req: ChatCompletionRequest, history: list[tuple[str, dict[str, Any]]]) -> ChatCompletionResponse:
            called = {name for name, _ in history}
            if "recall_expert_criteria" not in called:
                return make_tool_call_response("recall_expert_criteria", {}, call_id="call_o_1")
            if "list_recent_anomalies" not in called:
                return make_tool_call_response("list_recent_anomalies", {}, call_id="call_o_2")
            anom_data = next(res for name, res in history if name == "list_recent_anomalies")
            anomalies = anom_data.get("anomalies") or []
            evt_id = anomalies[0]["event_id"] if anomalies else "EVT-20261031-01"
            node_id = anomalies[0]["node_id"] if anomalies else "node-api-01"

            if "create_ticket" not in called:
                return make_tool_call_response(
                    "create_ticket",
                    {
                        "event_id": evt_id,
                        "category": "network",
                        "severity": "high",
                        "title": f"Gateway link interruption & telemetry freeze on {node_id}",
                        "summary": f"Cross-ID and expert criterion MEM-SEED-001 indicate upstream link freeze for {evt_id}.",
                    },
                    call_id="call_o_3",
                )
            if "propose_intent" not in called:
                return make_tool_call_response(
                    "propose_intent",
                    {
                        "kind": "reroute_traffic",
                        "target": node_id,
                        "trigger_event": evt_id,
                        "rationale": f"Drain and shift traffic away from {node_id} while link interruption {evt_id} is investigated.",
                        "parameters": {"drain_Grace_seconds": 30, "backup_node": "node-api-02"},
                    },
                    call_id="call_o_4",
                )
            tkt = next(res for name, res in history if name == "create_ticket")
            intent = next(res for name, res in history if name == "propose_intent")
            return make_text_response(
                f"### Operations Triage & Remediation Proposal Complete\n"
                f"- **Expert Memory Applied**: `link_freeze_with_upstream_packet_loss` (`watch`)\n"
                f"- **Ticket**: `{tkt.get('ticket_id')}` (Deduplicated: `{tkt.get('deduplicated')}`, Catalog: `{tkt.get('catalog_ref')}`)\n"
                f"- **Control Intent**: `{intent.get('intent_id')}` (`{intent.get('kind')}` on `{intent.get('target')}`, "
                f"Status: `{intent.get('status')}`, Catalog: `{intent.get('catalog_ref')}`)"
            )

        self.planner.register("query", plan_query)
        self.planner.register("report", plan_report)
        self.planner.register("rulecard", plan_rulecard)
        self.planner.register("ops", plan_ops)


def build_starter_pack(
    *,
    registry: ToolRegistry,
    gate: PolicyGate,
    store: GovernanceStore,
    memory: ExpertMemoryStore | None = None,
    planner: OfflinePlanner | None = None,
) -> StarterDomainPack:
    mem = memory if memory is not None else ExpertMemoryStore(DEFAULT_EXPERT_CRITERIA)
    pl = planner if planner is not None else OfflinePlanner()
    return StarterDomainPack(registry=registry, gate=gate, store=store, memory=mem, planner=pl)
