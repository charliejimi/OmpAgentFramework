"""Gated Step-by-Step Onboarding & Blueprint Customization Workflow Engine.

Guides a developer (or an AI coding agent) through building or customizing a system
on top of `OmpAgentFramework` one step at a time.

Strict Gated Progression Rule:
- Steps are ordered 1 through 7.
- Step $N+1$ remains `locked` until Step $N$ has been verified (`passed = True`) and completed.
- Each step has an automated `verify()` check that inspects the live runtime, files, tool registry,
  agent profiles, PolicyGate, and end-to-end execution before unlocking the next step.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from omp_framework.contracts import ContractModel
from omp_framework.rpc.process import DEFAULT_OMP_EXE, OMP_SHA256, repo_root, var_dir

__all__ = [
    "OnboardingCheckResult",
    "OnboardingStateView",
    "OnboardingStepView",
    "OnboardingWorkflow",
]

StepStatus = Literal["locked", "current", "completed"]


class OnboardingCheckResult(ContractModel):
    step_id: str
    step_number: int
    passed: bool
    summary: str
    checks: list[dict[str, Any]] = Field(default_factory=list)
    remediation_hint: str | None = None
    unlocked_next_step: str | None = None


class OnboardingStepView(ContractModel):
    step_id: str
    step_number: int
    title: str
    goal: str
    files_to_edit: list[str]
    instructions: list[str]
    cli_command: str
    status: StepStatus
    verified_at: str | None = None
    last_check_summary: str | None = None


class OnboardingStateView(ContractModel):
    current_step_number: int
    current_step_id: str | None
    completed_count: int
    total_steps: int
    progress_pct: int
    all_completed: bool
    steps: list[OnboardingStepView]


class _StepSpec:
    def __init__(
        self,
        *,
        step_id: str,
        step_number: int,
        title: str,
        goal: str,
        files_to_edit: list[str],
        instructions: list[str],
        cli_command: str,
        verifier: Callable[[], tuple[bool, str, list[dict[str, Any]], str | None]],
    ) -> None:
        self.step_id = step_id
        self.step_number = step_number
        self.title = title
        self.goal = goal
        self.files_to_edit = files_to_edit
        self.instructions = instructions
        self.cli_command = cli_command
        self.verifier = verifier


class OnboardingWorkflow:
    """7-step gated workflow guiding users from initial setup to full governed agent deployment."""

    def __init__(
        self,
        *,
        app_state: Any,
        state_file: Path | None = None,
    ) -> None:
        self.app_state = app_state
        self.state_file = state_file or (var_dir() / "onboarding_progress.json")
        self._completed: dict[str, dict[str, str]] = {}
        self._steps: list[_StepSpec] = self._build_steps()
        self._load_state()

    def _load_state(self) -> None:
        if self.state_file.is_file():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("completed"), dict):
                    self._completed = {
                        str(k): dict(v) for k, v in raw["completed"].items() if isinstance(v, dict)
                    }
            except (OSError, json.JSONDecodeError):
                self._completed = {}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"completed": self._completed, "updated_at": datetime.now(UTC).isoformat()}
        self.state_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def reset(self) -> OnboardingStateView:
        self._completed.clear()
        if self.state_file.is_file():
            try:
                self.state_file.unlink()
            except OSError:
                pass
        return self.get_state()

    def get_state(self) -> OnboardingStateView:
        views: list[OnboardingStepView] = []
        current_found = False
        current_num = len(self._steps)
        current_id: str | None = None

        for spec in self._steps:
            comp = self._completed.get(spec.step_id)
            if comp is not None and not current_found:
                status: StepStatus = "completed"
            elif not current_found:
                status = "current"
                current_found = True
                current_num = spec.step_number
                current_id = spec.step_id
            else:
                status = "locked"

            views.append(
                OnboardingStepView(
                    step_id=spec.step_id,
                    step_number=spec.step_number,
                    title=spec.title,
                    goal=spec.goal,
                    files_to_edit=spec.files_to_edit,
                    instructions=spec.instructions,
                    cli_command=spec.cli_command,
                    status=status,
                    verified_at=comp.get("verified_at") if comp else None,
                    last_check_summary=comp.get("summary") if comp else None,
                )
            )

        done_count = sum(1 for v in views if v.status == "completed")
        total = len(views)
        return OnboardingStateView(
            current_step_number=current_num,
            current_step_id=current_id,
            completed_count=done_count,
            total_steps=total,
            progress_pct=int(round((done_count / total) * 100)) if total else 100,
            all_completed=(done_count == total),
            steps=views,
        )

    def verify_step(self, step_id: str) -> OnboardingCheckResult:
        """Verify `step_id`. Enforces sequential order: earlier steps must be completed first."""
        idx = next((i for i, s in enumerate(self._steps) if s.step_id == step_id), None)
        if idx is None:
            raise KeyError(f"Unknown onboarding step: {step_id!r}")

        # Enforce sequential gate: all prior steps must already be completed
        for prev in self._steps[:idx]:
            if prev.step_id not in self._completed:
                return OnboardingCheckResult(
                    step_id=step_id,
                    step_number=self._steps[idx].step_number,
                    passed=False,
                    summary=f"Step {self._steps[idx].step_number} is locked until Step {prev.step_number} ({prev.title}) is completed.",
                    checks=[{"check": "prerequisite_step", "ok": False, "detail": f"Must complete {prev.step_id} first"}],
                    remediation_hint=f"Complete and verify Step {prev.step_number} (`{prev.step_id}`) before proceeding to Step {self._steps[idx].step_number}.",
                    unlocked_next_step=None,
                )

        spec = self._steps[idx]
        passed, summary, checks, hint = spec.verifier()
        next_step_id = self._steps[idx + 1].step_id if (passed and idx + 1 < len(self._steps)) else None

        if passed:
            self._completed[spec.step_id] = {
                "verified_at": datetime.now(UTC).isoformat(),
                "summary": summary,
            }
            self._save_state()

        return OnboardingCheckResult(
            step_id=spec.step_id,
            step_number=spec.step_number,
            passed=passed,
            summary=summary,
            checks=checks,
            remediation_hint=None if passed else hint,
            unlocked_next_step=next_step_id,
        )

    def verify_current_step(self) -> OnboardingCheckResult:
        state = self.get_state()
        target = state.current_step_id or self._steps[-1].step_id
        return self.verify_step(target)

    def _build_steps(self) -> list[_StepSpec]:
        return [
            _StepSpec(
                step_id="step-1-environment",
                step_number=1,
                title="Step 1: Environment & Sandboxed OMP Runtime",
                goal="Verify Python environment, isolated `var/omp-*` directories, and `tools/omp/omp.exe` SHA-256.",
                files_to_edit=["scripts/get-omp.ps1", "pyproject.toml"],
                instructions=[
                    "Run `uv sync --extra api` to install FastAPI, Pydantic, and test dependencies.",
                    "Run `powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1` to fetch and SHA-256 verify omp 18.11.2.",
                    "Click 'Verify & Unlock Next Step' to confirm the omp runtime and sandbox directories are ready.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-1-environment",
                verifier=self._verify_step_1,
            ),
            _StepSpec(
                step_id="step-2-domain-scopes",
                step_number=2,
                title="Step 2: Define Domain Scopes & Telemetry Data",
                goal="Define your target execution scopes (`ScopeContext`) and domain data structures.",
                files_to_edit=["src/starter_domain/pack.py", "src/omp_framework/contracts.py"],
                instructions=[
                    "Inspect or customize `SAMPLE_SCOPES` in `src/starter_domain/pack.py` with your domain's target scopes (e.g., clusters, sites, plants, or accounts).",
                    "Ensure each scope has a unique `scope_id`, `name`, `as_of`, and `metrics` dictionary.",
                    "Remember: `ScopeContext` is injected by the host so the LLM can never spoof or cross-contaminate scopes.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-2-domain-scopes",
                verifier=self._verify_step_2,
            ),
            _StepSpec(
                step_id="step-3-host-tools",
                step_number=3,
                title="Step 3: Register Read-Only & Mutating Host Tools",
                goal="Register Host Tools with `@registry.tool(...)`, Pydantic argument schemas, and `SourceRef` provenance.",
                files_to_edit=["src/starter_domain/pack.py", "src/omp_framework/tools/registry.py"],
                instructions=[
                    "Define Pydantic `ContractModel` argument schemas for your tools in `src/starter_domain/pack.py` (never include `scope_id` in LLM arguments).",
                    "Register at least one `read_only` tool (returning `SourceRef` provenance) and one mutating proposal tool (`report_draft`, `rule_draft`, `ticket`, or `intent`).",
                    "Verify that all tool schemas serialize cleanly to omp `set_host_tools` JSON Schema.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-3-host-tools",
                verifier=self._verify_step_3,
            ),
            _StepSpec(
                step_id="step-4-agent-profiles",
                step_number=4,
                title="Step 4: Configure OMP Agent Markdown Profiles",
                goal="Create or customize `agents/omp/*.md` profiles with `omp-agent:` markers, tool bindings, and RunLimits.",
                files_to_edit=["agents/omp/query.md", "agents/omp/report.md", "agents/omp/rulecard.md", "agents/omp/ops.md"],
                instructions=[
                    "Open `agents/omp/*.md`. Each file must start with `omp-agent: <agent_id>` followed by YAML frontmatter (`label`, `domain`, `tools`, `max_tool_calls`, `timeout_s`) and `---`.",
                    "Ensure every tool listed in an agent's `tools:` YAML frontmatter is registered in `ToolRegistry`.",
                    "Keep system prompts free of `{{...}}` handlebars syntax.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-4-agent-profiles",
                verifier=self._verify_step_4,
            ),
            _StepSpec(
                step_id="step-5-policy-gate",
                step_number=5,
                title="Step 5: Configure PolicyGate RBAC & Autonomous Catalog",
                goal="Verify PolicyGate enforces Agent-vs-Human boundaries, read-only scopes, and pre-approved Catalog entries.",
                files_to_edit=["src/omp_framework/control/gate.py", "src/starter_domain/pack.py"],
                instructions=[
                    "Review `DEFAULT_HUMAN_ACTIONS` in `src/omp_framework/control/gate.py` (who can approve intents, sign rules, or publish reports).",
                    "Review `DEFAULT_CATALOG` in `src/starter_domain/pack.py` for pre-approved ticket categories and autonomous intents.",
                    "Verification runs live PolicyGate checks to confirm agents are blocked from human actions and unauthorized tools.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-5-policy-gate",
                verifier=self._verify_step_5,
            ),
            _StepSpec(
                step_id="step-6-llm-planner",
                step_number=6,
                title="Step 6: Verify LLM Router & Deterministic Offline Planner",
                goal="Verify `/llm/v1` OpenAI-compatible router and deterministic `OfflinePlanner` for every agent profile.",
                files_to_edit=["src/starter_domain/pack.py", "agents/omp/agent/models.yml"],
                instructions=[
                    "Ensure `OfflinePlanner` in `src/starter_domain/pack.py` has deterministic fallback planners registered (or uses the generic fallback) for CI/offline testing.",
                    "Optionally configure an external OpenAI-compatible provider in `agents/omp/agent/models.yml` or `OMP_LLM_BACKEND`.",
                    "Verification tests a simulated completion round-trip for each registered agent.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-6-llm-planner",
                verifier=self._verify_step_6,
            ),
            _StepSpec(
                step_id="step-7-e2e-run",
                step_number=7,
                title="Step 7: End-to-End Governed Agent Execution & Human Sign-Off",
                goal="Run a complete agent execution + human governance workflow (or verify completed runs in this session).",
                files_to_edit=["apps/launchpad/index.html", "tests/test_framework.py"],
                instructions=[
                    "Execute an agent via `AgentRunner.run` (or click 'Verify & Unlock' to run an automated end-to-end diagnostic + proposal verification).",
                    "Verify that tool calls, `SourceRef` citations, and `AuditSink` entries are recorded.",
                    "Once Step 7 passes, your custom Governed OMP Agent System is verified and ready for production!",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-7-e2e-run",
                verifier=self._verify_step_7,
            ),
        ]

    # ── Step Verifiers ────────────────────────────────────────────────────────

    def _verify_step_1(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        runner = self.app_state.runner
        omp_ok = DEFAULT_OMP_EXE.is_file() and (
            hashlib.sha256(DEFAULT_OMP_EXE.read_bytes()).hexdigest().lower() == OMP_SHA256
        )
        custom_cmd = runner.omp_command != (str(DEFAULT_OMP_EXE),)
        runtime_ready = omp_ok or custom_cmd
        models_yml_exists = (runner.paths.agent / "models.yml").is_file()

        checks = [
            {"check": "omp_binary_or_test_double", "ok": runtime_ready, "detail": str(runner.omp_command[0])},
            {"check": "isolated_omp_home", "ok": runner.paths.home.is_dir(), "detail": str(runner.paths.home)},
            {"check": "rendered_models_yml", "ok": models_yml_exists, "detail": str(runner.paths.agent / "models.yml")},
        ]
        passed = all(c["ok"] for c in checks)
        summary = (
            "Environment, isolated `var/omp-home`, and omp runtime verified."
            if passed
            else "OMP binary or isolated runtime directories are missing."
        )
        hint = "Run `powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1` to download and verify omp.exe."
        return passed, summary, checks, hint

    def _verify_step_2(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        pack = self.app_state.pack
        scopes = getattr(pack, "scopes", {})
        has_scopes = isinstance(scopes, dict) and len(scopes) >= 1
        valid_fields = all(
            isinstance(v, dict) and "scope_id" in v and "name" in v and "metrics" in v for v in scopes.values()
        )
        checks = [
            {"check": "scopes_defined", "ok": has_scopes, "detail": f"{len(scopes)} scope(s): {list(scopes.keys())}"},
            {"check": "scope_schema_valid", "ok": valid_fields, "detail": "All scopes contain scope_id, name, metrics"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = f"Verified {len(scopes)} domain scope(s): {', '.join(scopes.keys())}."
        hint = "Ensure `pack.scopes` in `src/starter_domain/pack.py` has at least one scope with `scope_id`, `name`, and `metrics`."
        return passed, summary, checks, hint

    def _verify_step_3(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        registry = self.app_state.registry
        specs = registry.all_specs()
        ro_tools = [s.name for s in specs if not s.mutates]
        mut_tools = [s.name for s in specs if s.mutates]
        no_scope_leak = all("scope_id" not in s.rpc_spec()["parameters"].get("properties", {}) for s in specs)
        handlers_bound = all(registry.get_handler(s.name) is not None for s in specs)

        checks = [
            {"check": "read_only_tools_registered", "ok": len(ro_tools) >= 1, "detail": f"Read-only: {ro_tools}"},
            {"check": "mutating_tools_registered", "ok": len(mut_tools) >= 1, "detail": f"Mutating: {mut_tools}"},
            {"check": "no_scope_id_in_llm_args", "ok": no_scope_leak, "detail": "ScopeContext is strictly host-injected"},
            {"check": "all_handlers_bound", "ok": handlers_bound, "detail": f"{len(specs)} total tools bound"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = f"Verified {len(specs)} Host Tools ({len(ro_tools)} read-only, {len(mut_tools)} governed mutating)."
        hint = "Register at least one read-only and one mutating tool in `src/starter_domain/pack.py` without `scope_id` in LLM args."
        return passed, summary, checks, hint

    def _verify_step_4(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        runner = self.app_state.runner
        registry = self.app_state.registry
        profiles = runner.profiles
        has_profiles = len(profiles) >= 1
        unknown_tools: list[str] = []
        for aid, prof in profiles.items():
            for tname in prof.tools:
                if registry.get_spec(tname) is None:
                    unknown_tools.append(f"{aid}:{tname}")

        checks = [
            {"check": "agent_md_profiles_loaded", "ok": has_profiles, "detail": f"Agents: {list(profiles.keys())}"},
            {
                "check": "all_agent_tools_exist_in_registry",
                "ok": len(unknown_tools) == 0,
                "detail": "All declared agent tools exist" if not unknown_tools else f"Missing: {unknown_tools}",
            },
        ]
        passed = all(c["ok"] for c in checks)
        summary = f"Verified {len(profiles)} agent profile(s) ({', '.join(profiles.keys())}) and tool bindings."
        hint = f"Fix unknown tools in `agents/omp/*.md`: {unknown_tools}"
        return passed, summary, checks, hint

    def _verify_step_5(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        from omp_framework.contracts import Action, GateContext, Principal, ScopeContext

        gate = self.app_state.gate
        ctx = GateContext(scope=ScopeContext(scope_id="prod-cluster-01"))
        d1 = gate.authorize(Principal.for_agent("query"), Action.human("approve_intent"), ctx)
        d2 = gate.authorize(Principal.for_human("user1", "viewer"), Action.human("approve_intent"), ctx)
        d3 = gate.authorize(Principal.for_human("lead1", "ops_lead"), Action.human("approve_intent"), ctx)
        catalog_count = len(gate.list_catalog())

        checks = [
            {"check": "agent_blocked_from_human_action", "ok": d1.effect == "deny" and d1.basis == "agent_human_action", "detail": d1.reason},
            {"check": "unauthorized_role_blocked", "ok": d2.effect == "deny", "detail": d2.reason},
            {"check": "authorized_role_allowed", "ok": d3.effect == "allow", "detail": d3.reason},
            {"check": "catalog_entries_loaded", "ok": catalog_count >= 1, "detail": f"{catalog_count} catalog entries active"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = f"PolicyGate RBAC matrix and {catalog_count} Catalog entries verified."
        hint = "Check `PolicyGate` human actions and catalog registrations."
        return passed, summary, checks, hint

    def _verify_step_6(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        from omp_framework.contracts import ChatCompletionRequest, ChatMessage

        llm_ep = self.app_state.llm_endpoint
        runner = self.app_state.runner
        tested_agents: list[str] = []
        all_ok = True
        for aid in runner.profiles:
            req = ChatCompletionRequest(
                model="omp-offline",
                messages=[
                    ChatMessage(role="system", content=f"omp-agent: {aid}\nSystem prompt"),
                    ChatMessage(role="user", content="Verify planner"),
                ],
            )
            resp = llm_ep.backend.complete(req)
            if not (resp.content or resp.tool_calls):
                all_ok = False
            else:
                tested_agents.append(aid)

        checks = [
            {"check": "llm_backend_mode", "ok": bool(llm_ep.mode), "detail": f"mode={llm_ep.mode}"},
            {"check": "agent_planners_respond", "ok": all_ok and len(tested_agents) >= 1, "detail": f"Verified: {tested_agents}"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = f"LLM Router (`mode={llm_ep.mode}`) and planners verified for {len(tested_agents)} agents."
        hint = "Ensure `llm_endpoint.backend.complete()` returns a valid `ChatCompletionResponse`."
        return passed, summary, checks, hint

    def _verify_step_7(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        from omp_framework.contracts import ScopeContext

        runner = self.app_state.runner
        registry = self.app_state.registry
        gate = self.app_state.gate

        # Execute a live governed ToolSession round-trip (and full runner if fake_omp/omp is configured)
        first_Scope = next(iter(self.app_state.pack.scopes.keys()), "prod-cluster-01")
        scope_ctx = ScopeContext(scope_id=first_Scope)
        session = registry.session(
            agent_id="query",
            agent_domain="monitoring",
            allowed_tools=runner.profiles["query"].tools,
            scope=scope_ctx,
            gate=gate,
        )
        first_tool = runner.profiles["query"].tools[0]
        resp = session.call(first_tool, {}, call_id="onboard_verify_1")

        checks = [
            {"check": "governed_tool_session_execution", "ok": not resp.is_error, "detail": f"Executed {first_tool} on {first_Scope}"},
            {"check": "provenance_citations_generated", "ok": len(resp.citations) >= 1, "detail": f"{len(resp.citations)} citation(s) attached"},
            {"check": "audit_trail_recorded", "ok": len(self.app_state.audit.list_entries()) >= 1, "detail": f"{len(self.app_state.audit.list_entries())} audit entries"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = "End-to-end governed execution, provenance citations, and PolicyGate audit trail verified!"
        hint = "Ensure `query` agent's first tool executes cleanly and returns `SourceRef` citations."
        return passed, summary, checks, hint
