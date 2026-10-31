---
name: blueprint-onboarding
description: Guides users step-by-step through customizing OmpAgentFramework for their own domain using the gated 7-step OnboardingWorkflow (`python -m omp_framework.onboarding`). Enforces completing and verifying each step before moving to the next step.
---

# Blueprint Onboarding & Customization Workflow Skill

When a user asks how to use or customize `OmpAgentFramework` for their own project, **always guide them through the gated 7-step workflow one step at a time**. Never skip ahead to Step $N+1$ until Step $N$ has passed verification.

## How the Gated Workflow Works

Run the built-in CLI wizard (or use the **🧭 0. Step-by-Step Setup Wizard** tab in the Launchpad UI at `http://127.0.0.1:8710`):

```powershell
# Check current active step and instructions
.venv\Scripts\python -m omp_framework.onboarding status

# Verify the current step and unlock the next step
.venv\Scripts\python -m omp_framework.onboarding verify
```

## The 7 Gated Steps

1. **`step-1-environment` — Environment & Sandboxed OMP Runtime**
   - Verify `.venv`, `tools/omp/omp.exe` SHA-256, and isolated `var/omp-home/agent/models.yml`.
2. **`step-2-domain-scopes` — Define Domain Scopes & Telemetry Data**
   - Customize `SAMPLE_SCOPES` in `src/starter_domain/pack.py` with the user's target scopes (`scope_id`, `name`, `as_of`, `metrics`).
3. **`step-3-host-tools` — Register Read-Only & Mutating Host Tools**
   - Define Pydantic `ContractModel` schemas (never put `scope_id` in LLM tool arguments—it is injected via `inv.scope`) and register tools with `@registry.tool(...)`.
4. **`step-4-agent-profiles` — Configure OMP Agent Markdown Profiles**
   - Edit `agents/omp/*.md` (`omp-agent: <id>` marker + YAML frontmatter `tools`, `max_tool_calls`, `timeout_s` + `---` + system prompt).
5. **`step-5-policy-gate` — Configure PolicyGate RBAC & Autonomous Catalog**
   - Configure `DEFAULT_HUMAN_ACTIONS` in `src/omp_framework/control/gate.py` and `DEFAULT_CATALOG` in `src/starter_domain/pack.py`.
6. **`step-6-llm-planner` — Verify LLM Router & Deterministic Offline Planner**
   - Register deterministic fallback planners in `OfflinePlanner` or configure external LLM providers in `agents/omp/agent/models.yml`.
7. **`step-7-e2e-run` — End-to-End Governed Agent Execution & Human Sign-Off**
   - Execute a governed tool session and verify citations and audit trail entries.
