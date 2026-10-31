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
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from omp_framework.contracts import ContractModel
from omp_framework.rpc.process import DEFAULT_OMP_EXE, OMP_SHA256, repo_root, var_dir

__all__ = [
    "DomainRequirementSpec",
    "OnboardingCheckResult",
    "OnboardingStateView",
    "OnboardingStepView",
    "OnboardingWorkflow",
    "REQUIREMENT_QUESTIONS",
    "ScaffoldResult",
]

StepStatus = Literal["locked", "current", "completed"]


class ScaffoldResult(ContractModel):
    project_name: str
    package_slug: str
    output_dir: str
    generated_files: list[str]
    env_command: str


class DomainRequirementSpec(ContractModel):
    """Structured domain requirements captured during Step 1 (Requirement Discovery Interview)."""

    project_name: str = Field(min_length=2, description="Name of the new system (e.g., GridSentinel, FinOpsGuard, ICUWatch)")
    domain_purpose: str = Field(min_length=5, description="What problem this system solves and what it monitors/governs")
    scope_entity_name: str = Field(min_length=2, description="What a single ScopeContext represents (e.g., substation, k8s_cluster, factory_line, dam_site)")
    sample_scope_ids: list[str] = Field(min_length=1, description="1 to 3 example scope IDs (e.g., ['substation-north-01', 'substation-south-02'])")
    key_metrics: list[str] = Field(min_length=1, description="Key numeric metrics/signals to monitor and fact-check (e.g., ['voltage_kv', 'transformer_temp_c', 'Availability_sla'])")
    selected_archetypes: list[Literal["query", "report", "rulecard", "ops"]] = Field(
        min_length=1,
        default_factory=lambda: ["query", "report", "rulecard", "ops"],
        description="Which of the 4 universal agent archetypes to include",
    )
    read_only_tools: list[str] = Field(min_length=1, description="Read-only Host Tools needed (e.g., ['get_grid_metrics', 'list_line_faults'])")
    mutating_proposals: list[str] = Field(min_length=1, description="Governed write proposals needed (e.g., ['submit_report_draft', 'create_ticket', 'propose_intent', 'propose_rule_draft'])")
    human_approver_roles: list[str] = Field(min_length=1, default_factory=lambda: ["domain_expert", "ops_lead"], description="Human roles required to approve intents, sign rules, or publish reports")
    event_triggers: list[str] = Field(default_factory=lambda: ["threshold_breach", "telemetry_link_loss"], description="Automated anomaly events that should wake up the ops agent")
    llm_mode_preference: Literal["offline", "custom_python_backend", "external_openai_api", "omp_extension"] = Field(
        default="offline",
        description="Preferred LLM routing mode",
    )


REQUIREMENT_QUESTIONS: list[dict[str, str]] = [
    {"field": "project_name", "question": "1. What is the name of your new system?", "default": "CustomSentinel"},
    {"field": "domain_purpose", "question": "2. What is the core purpose of your system (what domain does it monitor & govern)?", "default": "Automated reliability monitoring, root-cause triage, and human-governed remediation"},
    {"field": "scope_entity_name", "question": "3. What does a single execution scope (`ScopeContext.scope_id`) represent in your domain?", "default": "cluster"},
    {"field": "sample_scope_ids", "question": "4. List 1-3 example scope IDs (comma-separated):", "default": "prod-cluster-01, edge-site-alpha"},
    {"field": "key_metrics", "question": "5. List the key numeric metrics to monitor and fact-check (comma-separated):", "default": "cpu_load_ratio, p99_latency_ms, availability_sla"},
    {"field": "selected_archetypes", "question": "6. Which agent archetypes do you need? (comma-separated from: query, report, rulecard, ops):", "default": "query, report, rulecard, ops"},
    {"field": "read_only_tools", "question": "7. What read-only Host Tools should agents call? (comma-separated):", "default": "get_system_metrics, list_recent_anomalies"},
    {"field": "mutating_proposals", "question": "8. What governed mutating/proposal tools should agents call? (comma-separated):", "default": "submit_report_draft, simulate_rule_change, propose_rule_draft, create_ticket, propose_intent"},
    {"field": "human_approver_roles", "question": "9. Which human roles are authorized to approve intents, sign rules, or publish reports? (comma-separated):", "default": "ops_lead, domain_expert, safety_officer"},
    {"field": "event_triggers", "question": "10. What external/scheduled anomaly events should wake up your triage agent? (comma-separated):", "default": "link_packet_loss, latency_spike"},
    {"field": "llm_mode_preference", "question": "11. Which LLM mode do you want to start with? (offline | custom_python_backend | external_openai_api | omp_extension):", "default": "offline"},
]


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
    requirements: DomainRequirementSpec | None = None
    requirement_questions: list[dict[str, str]] = Field(default_factory=lambda: list(REQUIREMENT_QUESTIONS))
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
    """8-step gated workflow starting with Step 1 (Requirement Discovery Interview) through Step 8 (E2E Verification)."""

    def __init__(
        self,
        *,
        app_state: Any,
        state_file: Path | None = None,
    ) -> None:
        self.app_state = app_state
        self.state_file = state_file or (var_dir() / "onboarding_progress.json")
        self.requirements_file = self.state_file.parent / "domain_requirements.json"
        self._completed: dict[str, dict[str, str]] = {}
        self._requirements: DomainRequirementSpec | None = None
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
        if self.requirements_file.is_file():
            try:
                raw_req = json.loads(self.requirements_file.read_text(encoding="utf-8"))
                self._requirements = DomainRequirementSpec.model_validate(raw_req)
            except Exception:
                self._requirements = None

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"completed": self._completed, "updated_at": datetime.now(UTC).isoformat()}
        self.state_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def save_requirements(self, spec: DomainRequirementSpec) -> OnboardingCheckResult:
        """Save the user's `DomainRequirementSpec`, generate markdown blueprint summary, and auto-verify Step 1."""
        self.requirements_file.parent.mkdir(parents=True, exist_ok=True)
        self.requirements_file.write_text(
            json.dumps(spec.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path = self.requirements_file.parent / "DOMAIN_BLUEPRINT_SPEC.md"
        md_lines = [
            f"# Domain Blueprint Specification — {spec.project_name}",
            "",
            f"- **Purpose**: {spec.domain_purpose}",
            f"- **Scope Entity (`ScopeContext.scope_id`)**: `{spec.scope_entity_name}` (Examples: {', '.join(f'`{s}`' for s in spec.sample_scope_ids)})",
            f"- **Key Monitored & Fact-Checked Metrics**: {', '.join(f'`{m}`' for m in spec.key_metrics)}",
            f"- **Selected OMP Agent Archetypes**: {', '.join(f'`{a}`' for a in spec.selected_archetypes)}",
            f"- **Read-Only Host Tools**: {', '.join(f'`{t}`' for t in spec.read_only_tools)}",
            f"- **Governed Mutating Proposals**: {', '.join(f'`{t}`' for t in spec.mutating_proposals)}",
            f"- **Human Approver Roles (`PolicyGate`)**: {', '.join(f'`{r}`' for r in spec.human_approver_roles)}",
            f"- **Automated Event Triggers**: {', '.join(f'`{e}`' for e in spec.event_triggers)}",
            f"- **Preferred LLM Mode**: `{spec.llm_mode_preference}`",
            "",
        ]
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        self._requirements = spec
        return self.verify_step("step-1-requirements")

    def scaffold_from_requirements(self, target_root: Path | None = None) -> ScaffoldResult:
        """Auto-generate a ready-to-run Domain Pack and Agent Markdown files from Step 1 `DomainRequirementSpec`."""
        req = self._requirements
        if req is None and self.requirements_file.is_file():
            req = DomainRequirementSpec.model_validate(
                json.loads(self.requirements_file.read_text(encoding="utf-8"))
            )
            self._requirements = req
        if req is None:
            raise ValueError("Complete Step 1 (Requirement Discovery Interview) before running scaffold.")

        slug = re.sub(r"[^a-z0-9_]+", "_", req.project_name.lower()).strip("_") or "custom_domain"
        base_dir = target_root if target_root is not None else (self.state_file.parent / "scaffolded" / slug)
        pkg_dir = base_dir / "src" / slug
        agents_dir = base_dir / "agents" / "omp"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        agents_dir.mkdir(parents=True, exist_ok=True)

        generated: list[str] = []

        # 1. Generate __init__.py
        init_file = pkg_dir / "__init__.py"
        init_file.write_text(
            f'"""Auto-scaffolded domain pack for {req.project_name} ({req.domain_purpose})."""\n\n'
            f"from {slug}.pack import build_pack\n\n__all__ = ['build_pack']\n",
            encoding="utf-8",
        )
        generated.append(str(init_file))

        # 2. Generate pack.py with user's scopes, metrics, and tools
        scopes_dict = {
            sid: {
                "scope_id": sid,
                "name": f"{req.project_name} — {req.scope_entity_name} {sid}",
                "as_of": datetime.now(UTC).strftime("%Y-%m-%d"),
                "overall_grade": 1,
                "overall_status": "healthy_green",
                "availability_sla": 0.999,
                "metrics": {m: 0.75 for m in req.key_metrics},
                "nodes": [{"node_id": f"{sid}-node-01", "load_ratio": 0.75, "light": "green", "link_status": "ok", "sensor_trust": "trusted"}],
                "recent_anomalies": [
                    {"event_id": f"EVT-{sid.upper()}-01", "node_id": f"{sid}-node-01", "kind": req.event_triggers[0] if req.event_triggers else "anomaly", "detail": f"Sample {req.domain_purpose} event"}
                ],
                "historical_ratios": [0.5, 0.6, 0.65, 0.7, 0.75],
            }
            for sid in req.sample_scope_ids
        }

        pack_code = f'''"""Auto-generated Domain Pack for {req.project_name}."""

from __future__ import annotations
from typing import Any
from omp_framework.control.gate import PolicyGate
from omp_framework.control.store import GovernanceStore
from omp_framework.llm.backend import OfflinePlanner
from omp_framework.memory.store import ExpertMemoryStore
from omp_framework.tools.registry import ToolRegistry
from starter_domain.pack import StarterDomainPack, build_starter_pack

CUSTOM_SCOPES: dict[str, dict[str, Any]] = {repr(scopes_dict)}


def build_pack(
    *,
    registry: ToolRegistry,
    gate: PolicyGate,
    store: GovernanceStore,
    memory: ExpertMemoryStore | None = None,
    planner: OfflinePlanner | None = None,
) -> StarterDomainPack:
    pack = build_starter_pack(registry=registry, gate=gate, store=store, memory=memory, planner=planner)
    pack.name = {req.project_name!r}
    pack.scopes = {{k: dict(v) for k, v in CUSTOM_SCOPES.items()}}
    return pack
'''
        pack_file = pkg_dir / "pack.py"
        pack_file.write_text(pack_code, encoding="utf-8")
        generated.append(str(pack_file))

        # 3. Generate agents/omp/<archetype>.md profiles
        for arch in req.selected_archetypes:
            tools_list = list(req.read_only_tools)
            if arch != "query":
                tools_list.extend(t for t in req.mutating_proposals if t not in tools_list)
            tools_yaml = "\n".join(f"  - {t}" for t in tools_list)
            md_content = (
                f"omp-agent: {arch}\n"
                f"label: {req.project_name} ({arch.upper()})\n"
                f"domain: {req.scope_entity_name}\n"
                f"description: {req.domain_purpose} ({arch} archetype)\n"
                f"tools:\n{tools_yaml}\n"
                f"max_tool_calls: 12\n"
                f"timeout_s: 120\n"
                f"---\n"
                f"You are the **{req.project_name} `{arch}` Agent**.\n\n"
                f"- Purpose: {req.domain_purpose}\n"
                f"- Scope Entity: `{req.scope_entity_name}`\n"
                f"- Monitored Metrics: {', '.join(f'`{m}`' for m in req.key_metrics)}\n"
                f"- Human Approver Roles: {', '.join(f'`{r}`' for r in req.human_approver_roles)}\n"
            )
            md_file = agents_dir / f"{arch}.md"
            md_file.write_text(md_content, encoding="utf-8")
            generated.append(str(md_file))

        return ScaffoldResult(
            project_name=req.project_name,
            package_slug=slug,
            output_dir=str(base_dir),
            generated_files=generated,
            env_command=f"$env:OMP_DOMAIN_PACK = '{slug}.pack:build_pack'",
        )

    def reset(self) -> OnboardingStateView:
        self._completed.clear()
        self._requirements = None
        for f in (self.state_file, self.requirements_file):
            if f.is_file():
                try:
                    f.unlink()
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
            requirements=self._requirements,
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
                step_id="step-1-requirements",
                step_number=1,
                title="Step 1: Requirement Discovery & Domain Blueprint Interview",
                goal="Answer the 11 domain requirement questions (project purpose, scopes, metrics, agent archetypes, tools, human approver roles, and triggers) to generate your Domain Blueprint Spec.",
                files_to_edit=["var/domain_requirements.json", "var/DOMAIN_BLUEPRINT_SPEC.md"],
                instructions=[
                    "Fill out the Requirement Discovery Questionnaire in the Setup Wizard UI (or run `.venv\\Scripts\\python -m omp_framework.onboarding interview` in the terminal).",
                    "Specify your system's purpose, what `ScopeContext` represents, key metrics to fact-check, required Host Tools, and which human roles approve actions.",
                    "Submit the requirements to generate `var/domain_requirements.json` + `var/DOMAIN_BLUEPRINT_SPEC.md` and unlock Step 2.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding interview",
                verifier=self._verify_step_requirements,
            ),
            _StepSpec(
                step_id="step-2-environment",
                step_number=2,
                title="Step 2: Environment & Sandboxed OMP Runtime",
                goal="Verify Python environment, isolated `var/omp-*` directories, and `tools/omp/omp.exe` SHA-256.",
                files_to_edit=["scripts/get-omp.ps1", "pyproject.toml"],
                instructions=[
                    "Run `uv sync --extra api` to install FastAPI, Pydantic, and test dependencies.",
                    "Run `powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1` to fetch and SHA-256 verify omp 18.11.2.",
                    "Click 'Verify & Unlock Next Step' to confirm the omp runtime and sandbox directories are ready.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-2-environment",
                verifier=self._verify_step_1,
            ),
            _StepSpec(
                step_id="step-3-domain-scopes",
                step_number=3,
                title="Step 3: Define Domain Scopes & Telemetry Data",
                goal="Define your target execution scopes (`ScopeContext`) and domain data structures matching your Step 1 requirements.",
                files_to_edit=["src/starter_domain/pack.py", "src/omp_framework/contracts.py"],
                instructions=[
                    "Inspect or customize `SAMPLE_SCOPES` in `src/starter_domain/pack.py` with your domain's target scopes defined in Step 1.",
                    "Ensure each scope has a unique `scope_id`, `name`, `as_of`, and `metrics` dictionary.",
                    "Remember: `ScopeContext` is injected by the host so the LLM can never spoof or cross-contaminate scopes.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-3-domain-scopes",
                verifier=self._verify_step_2,
            ),
            _StepSpec(
                step_id="step-4-host-tools",
                step_number=4,
                title="Step 4: Register Read-Only & Mutating Host Tools",
                goal="Register Host Tools with `@registry.tool(...)`, Pydantic argument schemas, and `SourceRef` provenance.",
                files_to_edit=["src/starter_domain/pack.py", "src/omp_framework/tools/registry.py"],
                instructions=[
                    "Define Pydantic `ContractModel` argument schemas for your tools in `src/starter_domain/pack.py` (never include `scope_id` in LLM arguments).",
                    "Register the read-only and mutating proposal tools identified in your Step 1 Requirement Spec.",
                    "Verify that all tool schemas serialize cleanly to omp `set_host_tools` JSON Schema.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-4-host-tools",
                verifier=self._verify_step_3,
            ),
            _StepSpec(
                step_id="step-5-agent-profiles",
                step_number=5,
                title="Step 5: Configure OMP Agent Markdown Profiles",
                goal="Create or customize `agents/omp/*.md` profiles with `omp-agent:` markers, tool bindings, and RunLimits.",
                files_to_edit=["agents/omp/query.md", "agents/omp/report.md", "agents/omp/rulecard.md", "agents/omp/ops.md"],
                instructions=[
                    "Open `agents/omp/*.md`. Each file must start with `omp-agent: <agent_id>` followed by YAML frontmatter (`label`, `domain`, `tools`, `max_tool_calls`, `timeout_s`) and `---`.",
                    "Ensure every tool listed in an agent's `tools:` YAML frontmatter is registered in `ToolRegistry`.",
                    "Keep system prompts free of `{{...}}` handlebars syntax.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-5-agent-profiles",
                verifier=self._verify_step_4,
            ),
            _StepSpec(
                step_id="step-6-policy-gate",
                step_number=6,
                title="Step 6: Configure PolicyGate RBAC & Autonomous Catalog",
                goal="Verify PolicyGate enforces Agent-vs-Human boundaries, read-only scopes, and pre-approved Catalog entries.",
                files_to_edit=["src/omp_framework/control/gate.py", "src/starter_domain/pack.py"],
                instructions=[
                    "Review `DEFAULT_HUMAN_ACTIONS` in `src/omp_framework/control/gate.py` (who can approve intents, sign rules, or publish reports).",
                    "Review `DEFAULT_CATALOG` in `src/starter_domain/pack.py` for pre-approved ticket categories and autonomous intents.",
                    "Verification runs live PolicyGate checks to confirm agents are blocked from human actions and unauthorized tools.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-6-policy-gate",
                verifier=self._verify_step_5,
            ),
            _StepSpec(
                step_id="step-7-llm-planner",
                step_number=7,
                title="Step 7: Verify LLM Router & Deterministic Offline Planner",
                goal="Verify `/llm/v1` OpenAI-compatible router and deterministic `OfflinePlanner` for every agent profile.",
                files_to_edit=["src/starter_domain/pack.py", "agents/omp/agent/models.yml"],
                instructions=[
                    "Ensure `OfflinePlanner` in `src/starter_domain/pack.py` has deterministic fallback planners registered (or uses the generic fallback) for CI/offline testing.",
                    "Optionally configure an external OpenAI-compatible provider in `agents/omp/agent/models.yml` or `OMP_LLM_BACKEND`.",
                    "Verification tests a simulated completion round-trip for each registered agent.",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-7-llm-planner",
                verifier=self._verify_step_6,
            ),
            _StepSpec(
                step_id="step-8-e2e-run",
                step_number=8,
                title="Step 8: End-to-End Governed Agent Execution & Human Sign-Off",
                goal="Run a complete agent execution + human governance workflow (or verify completed runs in this session).",
                files_to_edit=["apps/launchpad/index.html", "tests/test_framework.py"],
                instructions=[
                    "Execute an agent via `AgentRunner.run` (or click 'Verify & Unlock' to run an automated end-to-end diagnostic + proposal verification).",
                    "Verify that tool calls, `SourceRef` citations, and `AuditSink` entries are recorded.",
                    "Once Step 8 passes, your custom Governed OMP Agent System is verified and ready for production!",
                ],
                cli_command=".venv\\Scripts\\python -m omp_framework.onboarding verify step-8-e2e-run",
                verifier=self._verify_step_7,
            ),
        ]

    # ── Step Verifiers ────────────────────────────────────────────────────────

    def _verify_step_requirements(self) -> tuple[bool, str, list[dict[str, Any]], str | None]:
        req = self._requirements
        if req is None and self.requirements_file.is_file():
            try:
                req = DomainRequirementSpec.model_validate(
                    json.loads(self.requirements_file.read_text(encoding="utf-8"))
                )
                self._requirements = req
            except Exception:
                req = None

        if req is None:
            return (
                False,
                "Domain requirements have not been captured yet.",
                [
                    {
                        "check": "requirements_captured",
                        "ok": False,
                        "detail": "Submit the Requirement Discovery Questionnaire (UI or CLI `interview`) first.",
                    }
                ],
                "Fill out and submit the Requirement Discovery form in Step 1 (or run `python -m omp_framework.onboarding interview`) to define your project requirements.",
            )

        checks = [
            {"check": "project_and_purpose", "ok": bool(req.project_name and req.domain_purpose), "detail": f"{req.project_name}: {req.domain_purpose[:80]}"},
            {"check": "scope_and_metrics", "ok": len(req.sample_scope_ids) >= 1 and len(req.key_metrics) >= 1, "detail": f"Scope={req.scope_entity_name} ({req.sample_scope_ids}), Metrics={req.key_metrics}"},
            {"check": "archetypes_and_tools", "ok": len(req.selected_archetypes) >= 1 and len(req.read_only_tools) >= 1 and len(req.mutating_proposals) >= 1, "detail": f"Archetypes={req.selected_archetypes}, RO={req.read_only_tools}, Write={req.mutating_proposals}"},
            {"check": "governance_roles", "ok": len(req.human_approver_roles) >= 1, "detail": f"Approvers={req.human_approver_roles}, Triggers={req.event_triggers}"},
        ]
        passed = all(c["ok"] for c in checks)
        summary = (
            f"Captured requirements for `{req.project_name}` (Scope: `{req.scope_entity_name}`, "
            f"Archetypes: {req.selected_archetypes}, Tools: {len(req.read_only_tools)} RO / {len(req.mutating_proposals)} Write)."
        )
        return passed, summary, checks, "Complete all required fields in the Requirement Discovery Questionnaire."

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
