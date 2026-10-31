---
name: blueprint-onboarding
description: Interviews the user to capture domain requirements first (Step 1), then guides them step-by-step through customizing OmpAgentFramework using the gated 8-step OnboardingWorkflow (`python -m omp_framework.onboarding`). Enforces completing and verifying each step before moving to the next step.
---

# Blueprint Onboarding & Requirement Discovery Workflow Skill

When a user wants to build a new system or customize `OmpAgentFramework` for their own domain:
1. **ALWAYS start at Step 1 by interviewing the user for their domain requirements** (using `ask_user` / `ask_questions`, the Launchpad UI form, or `python -m omp_framework.onboarding interview`).
2. **Never write domain code or skip ahead to Step $N+1$ until Step $N$ has passed verification.**

## Step 1: Requirement Discovery Questions (Ask the User First!)

Before modifying any files, ask the user for:
1. **`project_name`**: Name of the new system (e.g., `GridSentinel`, `FinOpsGuard`, `ICUWatch`).
2. **`domain_purpose`**: What problem the system solves and what it monitors/governs.
3. **`scope_entity_name`**: What a single `ScopeContext.scope_id` represents (e.g., `substation`, `k8s_cluster`, `factory_line`).
4. **`sample_scope_ids`**: 1 to 3 example scope IDs.
5. **`key_metrics`**: Key numeric metrics/signals to monitor and deterministically fact-check.
6. **`selected_archetypes`**: Which of the 4 universal agent archetypes to include (`query`, `report`, `rulecard`, `ops`).
7. **`read_only_tools`**: Read-only Host Tools the agents need.
8. **`mutating_proposals`**: Governed write/proposal tools needed (`submit_report_draft`, `simulate_rule_change`, `propose_rule_draft`, `create_ticket`, `propose_intent`, `retain_expert_criterion`).
9. **`human_approver_roles`**: Human roles required to approve intents, sign rules, or publish reports.
10. **`event_triggers`**: External/scheduled anomaly events that should wake up the triage agent.
11. **`llm_mode_preference`**: `offline`, `custom_python_backend`, `external_openai_api`, or `omp_extension`.

Once collected, save the requirements via `POST /api/v1/onboarding/requirements` or `.venv\Scripts\python -m omp_framework.onboarding interview`, which generates `var/domain_requirements.json` and `var/DOMAIN_BLUEPRINT_SPEC.md` and unlocks Step 2.

## The 8 Gated Steps

1. **`step-1-requirements` — Requirement Discovery & Domain Blueprint Interview**
   - Interview user, save `DomainRequirementSpec`, and generate `var/DOMAIN_BLUEPRINT_SPEC.md`.
2. **`step-2-environment` — Environment & Sandboxed OMP Runtime**
   - Verify `.venv`, `tools/omp/omp.exe` SHA-256, and isolated `var/omp-home/agent/models.yml`.
3. **`step-3-domain-scopes` — Define Domain Scopes & Telemetry Data**
   - Customize `SAMPLE_SCOPES` in `src/starter_domain/pack.py` with the user's target scopes (`scope_id`, `name`, `as_of`, `metrics`).
4. **`step-4-host-tools` — Register Read-Only & Mutating Host Tools**
   - Define Pydantic `ContractModel` schemas (never put `scope_id` in LLM tool arguments—it is injected via `inv.scope`) and register tools with `@registry.tool(...)`.
5. **`step-5-agent-profiles` — Configure OMP Agent Markdown Profiles**
   - Edit `agents/omp/*.md` (`omp-agent: <id>` marker + YAML frontmatter `tools`, `max_tool_calls`, `timeout_s` + `---` + system prompt).
6. **`step-6-policy-gate` — Configure PolicyGate RBAC & Autonomous Catalog**
   - Configure `DEFAULT_HUMAN_ACTIONS` in `src/omp_framework/control/gate.py` and `DEFAULT_CATALOG` in `src/starter_domain/pack.py`.
7. **`step-7-llm-planner` — Verify LLM Router & Deterministic Offline Planner**
   - Register deterministic fallback planners in `OfflinePlanner` or configure external LLM providers in `agents/omp/agent/models.yml`.
8. **`step-8-e2e-run` — End-to-End Governed Agent Execution & Human Sign-Off**
   - Execute a governed tool session and verify citations and audit trail entries.
