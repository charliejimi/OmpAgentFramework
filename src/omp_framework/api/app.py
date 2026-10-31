"""FastAPI Application Factory & REST/Governance Endpoints for OmpAgentFramework."""

from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from omp_framework.contracts import (
    Action,
    GateContext,
    Principal,
    ScopeContext,
)
from omp_framework.control.gate import AuditSink, PolicyGate
from omp_framework.control.store import GovernanceStore
from omp_framework.llm.backend import LlmBackend, OfflinePlanner, load_backend
from omp_framework.llm.endpoint import MOUNT_PREFIX, create_llm_endpoint
from omp_framework.memory.store import ExpertMemoryStore
from omp_framework.onboarding.engine import DomainRequirementSpec, OnboardingWorkflow
from omp_framework.rpc.process import (
    DEFAULT_OMP_EXE,
    OMP_SHA256,
    OMP_VERSION,
    OmpPaths,
    repo_root,
)
from omp_framework.runner.runner import AgentRunner
from omp_framework.tools.registry import ToolRegistry
from omp_framework.triggers.bus import EventTriggerBus
from starter_domain.pack import DEFAULT_EXPERT_CRITERIA, build_starter_pack

__all__ = ["DOMAIN_PACK_ENV", "create_app", "load_domain_pack"]

DOMAIN_PACK_ENV: Final = "OMP_DOMAIN_PACK"


def load_domain_pack(
    *,
    registry: ToolRegistry,
    gate: PolicyGate,
    store: GovernanceStore,
    memory: ExpertMemoryStore,
    planner: OfflinePlanner,
    pack_spec: str | None = None,
) -> Any:
    """Load a pluggable Domain Pack from `OMP_DOMAIN_PACK=<module>:<factory_fn>` or default `starter_domain.pack:build_starter_pack`."""
    spec = (pack_spec or os.environ.get(DOMAIN_PACK_ENV, "")).strip()
    if not spec:
        return build_starter_pack(
            registry=registry,
            gate=gate,
            store=store,
            memory=memory,
            planner=planner,
        )
    if ":" not in spec:
        raise ValueError(f"{DOMAIN_PACK_ENV} must be '<module>:<factory_fn>', got {spec!r}")
    mod_name, fn_name = spec.split(":", 1)
    module = importlib.import_module(mod_name)
    factory = getattr(module, fn_name)
    return factory(
        registry=registry,
        gate=gate,
        store=store,
        memory=memory,
        planner=planner,
    )


def _envelope(data: Any, *, scope_id: str = "default", mode: str = "offline") -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(),
            "scope_id": scope_id,
            "llm_mode": mode,
            "framework": "omp-agent-framework",
        },
    }


def _check_omp_status(omp_exe: Path) -> dict[str, Any]:
    if not omp_exe.is_file():
        return {
            "status": "missing",
            "version": OMP_VERSION,
            "path": str(omp_exe),
            "detail": "Run `powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1` to download omp.",
        }
    sha = hashlib.sha256(omp_exe.read_bytes()).hexdigest().lower()
    if sha != OMP_SHA256:
        return {
            "status": "hash_mismatch",
            "version": OMP_VERSION,
            "path": str(omp_exe),
            "detail": f"SHA-256 mismatch (got {sha[:12]}...)",
        }
    return {
        "status": "ok",
        "version": OMP_VERSION,
        "path": str(omp_exe),
        "detail": "omp 18.11.2 verified and ready",
    }


# ── API Request Schemas ───────────────────────────────────────────────────────


class AgentAskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    scope_id: str = Field(default="prod-cluster-01")
    as_of: str = Field(default="2026-10-31")
    read_only: bool = Field(default=False)
    resume_session: str | None = Field(default=None, description="Optional session file path to resume multi-turn conversation")


class IntentReviewRequest(BaseModel):
    approve: bool
    user_id: str = Field(default="alice-ops")
    role: str = Field(default="ops_lead")
    review_note: str = Field(default="")


class RuleSignRequest(BaseModel):
    user_id: str = Field(default="dr-chen")
    role: str = Field(default="domain_expert")


class ReportPublishRequest(BaseModel):
    user_id: str = Field(default="dr-chen")
    role: str = Field(default="domain_expert")


class AutonomousToggleRequest(BaseModel):
    enabled: bool
    user_id: str = Field(default="alice-ops")
    role: str = Field(default="ops_lead")


class TriggerEmitRequest(BaseModel):
    event_id: str = Field(default="EVT-20261031-01")
    scope_id: str = Field(default="prod-cluster-01")
    source: str = Field(default="gateway-probe-cron")
    event_kind: str = Field(default="link_packet_loss")
    target_agent: str = Field(default="ops")
    severity: Literal["info", "warning", "critical"] = Field(default="warning")
    payload: dict[str, Any] = Field(default_factory=lambda: {"node_id": "node-api-01", "loss_pct": 100.0})


def create_app(
    *,
    port: int | None = None,
    omp_command: Sequence[str] | None = None,
    omp_paths: OmpPaths | None = None,
    llm_backend: LlmBackend | None = None,
) -> FastAPI:
    """Create and wire the FastAPI server with `/llm/v1`, `/api/v1`, Starter Domain Pack, and Launchpad UI."""
    api_port = port or int(os.environ.get("OMP_API_PORT", "8710"))
    llm_base_url = f"http://127.0.0.1:{api_port}{MOUNT_PREFIX}"

    audit = AuditSink()
    registry = ToolRegistry()
    gate = PolicyGate(audit=audit, tool_registry=registry)
    store = GovernanceStore(gate=gate)
    memory = ExpertMemoryStore(list(DEFAULT_EXPERT_CRITERIA))

    planner = OfflinePlanner()
    pack = load_domain_pack(
        registry=registry,
        gate=gate,
        store=store,
        memory=memory,
        planner=planner,
    )

    backend = llm_backend if llm_backend is not None else load_backend(default=planner)
    llm_ep = create_llm_endpoint(backend=backend)

    runner = AgentRunner(
        llm_base_url=llm_base_url,
        tool_registry=registry,
        gate=gate,
        omp_Paths=omp_paths,
        omp_command=omp_command,
        mode_getter=lambda: llm_ep.mode,
    )

    trigger_bus = EventTriggerBus(
        run_agent=lambda agent_id, prompt, scope_ctx: runner.run(agent_id, prompt, scope=scope_ctx)
    )

    app = FastAPI(
        title="OmpAgentFramework",
        version="0.1.0",
        description="General-Purpose Governed Agent Framework powered by omp (oh-my-pi RPC)",
    )
    app.state.audit = audit
    app.state.registry = registry
    app.state.gate = gate
    app.state.store = store
    app.state.memory = memory
    app.state.pack = pack
    app.state.llm_endpoint = llm_ep
    app.state.runner = runner
    app.state.trigger_bus = trigger_bus
    state_file = (omp_paths.home.parent / "onboarding_progress.json") if omp_paths is not None else None
    onboarding = OnboardingWorkflow(app_state=app.state, state_file=state_file)
    app.state.onboarding = onboarding

    # Mount OpenAI-compatible `/llm/v1` router for omp
    app.include_router(llm_ep.router, prefix=MOUNT_PREFIX)

    @app.get("/", response_class=HTMLResponse)
    async def launchpad_ui() -> HTMLResponse:
        ui_path = repo_root() / "apps" / "launchpad" / "index.html"
        if ui_path.is_file():
            return HTMLResponse(ui_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>OmpAgentFramework Launchpad</h1>")

    @app.get("/api/v1/health")
    async def get_health() -> dict[str, Any]:
        omp_info = _check_omp_status(DEFAULT_OMP_EXE)
        status = "ok" if omp_info["status"] == "ok" or omp_command is not None else "degraded"
        return _envelope(
            {
                "status": status,
                "domain": pack.name,
                "omp": omp_info,
                "llm_mode": runner.current_mode,
                "autonomous_enabled": gate.autonomous_enabled,
                "agents": list(runner.profiles.keys()),
                "scopes": list(pack.scopes.keys()),
            },
            mode=runner.current_mode,
        )

    @app.get("/api/v1/launchpad")
    async def get_launchpad(scope_id: str = Query(default="prod-cluster-01")) -> dict[str, Any]:
        scope_data = pack.get_scope_data(scope_id)
        return _envelope(
            {
                "domain": pack.name,
                "scope": scope_data,
                "scopes": [
                    {"scope_id": k, "name": v["name"], "overall_status": v["overall_status"]}
                    for k, v in pack.scopes.items()
                ],
                "agents": [
                    {
                        "agent_id": p.agent_id,
                        "label": p.label,
                        "domain": p.domain,
                        "description": p.description,
                        "tools": list(p.tools),
                        "max_tool_calls": p.max_tool_calls,
                        "timeout_s": p.timeout_s,
                    }
                    for p in runner.profiles.values()
                ],
                "autonomous_enabled": gate.autonomous_enabled,
                "active_rule": store.get_active_rule(scope_id),
                "intents": [i.model_dump() for i in store.list_intents(scope_id=scope_id)],
                "tickets": [t.model_dump() for t in store.list_tickets(scope_id=scope_id)],
                "reports": [r.model_dump() for r in store.list_reports(scope_id=scope_id)],
                "rule_drafts": [d.model_dump() for d in store.list_rule_drafts(scope_id=scope_id)],
                "triggers": [e.model_dump() for e in trigger_bus.list_events(scope_id=scope_id)],
                "memory": [m.model_dump() for m in memory.recall()],
                "catalog": [c.model_dump() for c in gate.list_catalog()],
                "audit": [a.model_dump() for a in audit.list_entries(limit=30)],
                "onboarding": onboarding.get_state().model_dump(),
            },
            scope_id=scope_id,
            mode=runner.current_mode,
        )

    @app.get("/api/v1/agents")
    async def list_agents() -> dict[str, Any]:
        return _envelope(
            [
                {
                    "agent_id": p.agent_id,
                    "label": p.label,
                    "domain": p.domain,
                    "description": p.description,
                    "tools": list(p.tools),
                    "max_tool_calls": p.max_tool_calls,
                    "timeout_s": p.timeout_s,
                }
                for p in runner.profiles.values()
            ],
            mode=runner.current_mode,
        )

    @app.get("/api/v1/tools")
    async def list_tools() -> dict[str, Any]:
        return _envelope(
            [
                {
                    "name": s.name,
                    "label": s.label,
                    "description": s.description,
                    "domain": s.domain,
                    "rule": s.rule,
                    "mutates": s.mutates,
                    "parameters": s.rpc_spec()["parameters"],
                }
                for s in registry.all_specs()
            ],
            mode=runner.current_mode,
        )

    @app.post("/api/v1/agents/{agent_id}/ask")
    async def ask_agent(agent_id: str, req: AgentAskRequest) -> Response:
        if agent_id not in runner.profiles:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
        try:
            runner.validate_prompt(req.prompt)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        scope_ctx = ScopeContext(
            scope_id=req.scope_id,
            as_of=req.as_of,
            read_only=req.read_only,
        )
        ans = await run_in_threadpool(
            runner.run,
            agent_id,
            req.prompt,
            scope=scope_ctx,
            resume_session=req.resume_session,
        )
        return JSONResponse(_envelope(ans.model_dump(), scope_id=req.scope_id, mode=runner.current_mode))

    @app.get("/api/v1/intents")
    async def list_intents(scope_id: str | None = None) -> dict[str, Any]:
        return _envelope(
            [i.model_dump() for i in store.list_intents(scope_id=scope_id)],
            scope_id=scope_id or "all",
            mode=runner.current_mode,
        )

    @app.post("/api/v1/intents/{intent_id}/review")
    async def review_intent(intent_id: str, req: IntentReviewRequest) -> Response:
        principal = Principal.for_human(req.user_id, role=req.role, domain="operations")
        try:
            updated = store.review_intent(
                intent_id,
                principal=principal,
                approve=req.approve,
                review_note=req.review_note,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        return JSONResponse(_envelope(updated.model_dump(), scope_id=updated.scope_id, mode=runner.current_mode))

    @app.post("/api/v1/control/autonomous")
    async def toggle_autonomous(req: AutonomousToggleRequest) -> Response:
        principal = Principal.for_human(req.user_id, role=req.role, domain="operations")
        decision = gate.authorize(
            principal,
            Action.human("toggle_autonomous_mode", domain="operations"),
            GateContext(scope=ScopeContext(scope_id="global"), arguments={"enabled": req.enabled}),
        )
        if decision.effect != "allow":
            return JSONResponse(status_code=403, content={"error": decision.reason})
        gate.autonomous_enabled = req.enabled
        return JSONResponse(_envelope({"autonomous_enabled": gate.autonomous_enabled}, mode=runner.current_mode))

    @app.get("/api/v1/tickets")
    async def list_tickets(scope_id: str | None = None) -> dict[str, Any]:
        return _envelope(
            [t.model_dump() for t in store.list_tickets(scope_id=scope_id)],
            scope_id=scope_id or "all",
            mode=runner.current_mode,
        )

    @app.get("/api/v1/reports")
    async def list_reports(scope_id: str | None = None) -> dict[str, Any]:
        return _envelope(
            [r.model_dump() for r in store.list_reports(scope_id=scope_id)],
            scope_id=scope_id or "all",
            mode=runner.current_mode,
        )

    @app.post("/api/v1/reports/{report_id}/publish")
    async def publish_report(report_id: str, req: ReportPublishRequest) -> Response:
        principal = Principal.for_human(req.user_id, role=req.role, domain="reporting")
        try:
            updated = store.publish_report(report_id, principal=principal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        return JSONResponse(_envelope(updated.model_dump(), scope_id=updated.scope_id, mode=runner.current_mode))

    @app.get("/api/v1/rules")
    async def list_rules(scope_id: str = Query(default="prod-cluster-01")) -> dict[str, Any]:
        return _envelope(
            {
                "active_rule": store.get_active_rule(scope_id),
                "drafts": [d.model_dump() for d in store.list_rule_drafts(scope_id=scope_id)],
            },
            scope_id=scope_id,
            mode=runner.current_mode,
        )

    @app.post("/api/v1/rules/{draft_id}/sign")
    async def sign_rule(draft_id: str, req: RuleSignRequest) -> Response:
        principal = Principal.for_human(req.user_id, role=req.role, domain="policy")
        try:
            updated = store.sign_rule_draft(draft_id, principal=principal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        return JSONResponse(_envelope(updated.model_dump(), scope_id=updated.scope_id, mode=runner.current_mode))

    @app.get("/api/v1/triggers")
    async def list_triggers(scope_id: str | None = None) -> dict[str, Any]:
        return _envelope(
            [e.model_dump() for e in trigger_bus.list_events(scope_id=scope_id)],
            scope_id=scope_id or "all",
            mode=runner.current_mode,
        )

    @app.post("/api/v1/triggers/emit")
    async def emit_trigger(req: TriggerEmitRequest) -> dict[str, Any]:
        record = await run_in_threadpool(
            trigger_bus.emit,
            event_id=req.event_id,
            scope_id=req.scope_id,
            source=req.source,
            event_kind=req.event_kind,
            target_agent=req.target_agent,
            severity=req.severity,
            payload=req.payload,
        )
        return _envelope(record.model_dump(), scope_id=req.scope_id, mode=runner.current_mode)

    @app.get("/api/v1/memory")
    async def list_memory() -> dict[str, Any]:
        return _envelope([m.model_dump() for m in memory.recall()], mode=runner.current_mode)

    @app.get("/api/v1/audit")
    async def list_audit(scope_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        return _envelope(
            [a.model_dump() for a in audit.list_entries(scope_id=scope_id, limit=limit)],
            scope_id=scope_id or "all",
            mode=runner.current_mode,
        )

    @app.get("/api/v1/onboarding")
    async def get_onboarding() -> dict[str, Any]:
        return _envelope(onboarding.get_state().model_dump(), mode=runner.current_mode)

    @app.post("/api/v1/onboarding/requirements")
    async def submit_onboarding_requirements(spec: DomainRequirementSpec) -> Response:
        res = await run_in_threadpool(onboarding.save_requirements, spec)
        status_code = 200 if res.passed else 409
        return JSONResponse(
            status_code=status_code,
            content=_envelope(
                {
                    "result": res.model_dump(),
                    "state": onboarding.get_state().model_dump(),
                },
                mode=runner.current_mode,
            ),
        )

    @app.post("/api/v1/onboarding/scaffold")
    async def scaffold_onboarding_domain() -> Response:
        try:
            scaf = await run_in_threadpool(onboarding.scaffold_from_requirements)
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        return JSONResponse(_envelope(scaf.model_dump(), mode=runner.current_mode))

    @app.post("/api/v1/onboarding/steps/{step_id}/verify")
    async def verify_onboarding_step(step_id: str) -> Response:
        try:
            res = await run_in_threadpool(onboarding.verify_step, step_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        status_code = 200 if res.passed else 409
        return JSONResponse(
            status_code=status_code,
            content=_envelope(
                {
                    "result": res.model_dump(),
                    "state": onboarding.get_state().model_dump(),
                },
                mode=runner.current_mode,
            ),
        )

    @app.post("/api/v1/onboarding/reset")
    async def reset_onboarding() -> dict[str, Any]:
        state = onboarding.reset()
        return _envelope(state.model_dump(), mode=runner.current_mode)

    return app
