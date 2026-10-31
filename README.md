# 🛡️ OmpAgentFramework Blueprint

A **General-Purpose Governed AI Agent Framework** where [`omp` (`oh-my-pi` v18.11.2)](https://github.com/can1357/oh-my-pi) runs as a sandboxed RPC agent engine under strict **PolicyGate RBAC**, **Host-Injected Scope Isolation**, **Deterministic Fact-Checking**, **Human-in-the-Loop Approvals**, **Event Triggers**, and **Long-Term Expert Memory**.

Extracted from the HydroSentinel architecture into a clean, domain-agnostic blueprint repository so any team can build high-trust agentic systems (e.g., SRE/Cloud Ops, Smart Manufacturing, FinOps, Healthcare Monitoring, Telecom NOC, or Civil Infrastructure) in minutes.

---

## ✨ What This Blueprint Gives You Out of the Box

| Layer | Module | Capability |
|---|---|---|
| **1. Sandboxed `omp` RPC Engine** | `src/omp_framework/rpc/` | Spawns `omp --mode rpc --no-tools --no-extensions ...` over NDJSON stdio (Protocol v2 with `rpc_chunk` 64 MiB reassembly), strips all secret/token env vars, isolates `HOME`/`USERPROFILE` under `var/omp-home/`, limits concurrency, and enforces per-agent `max_tool_calls` & `timeout_s`. |
| **2. Pluggable Dual-Brain LLM Router** | `src/omp_framework/llm/` | Mounts an OpenAI-compatible `/llm/v1/chat/completions` (SSE + JSON) proxy that routes `omp` requests to: (a) a deterministic **`OfflinePlanner`** (zero API keys needed for CI/CD or air-gapped environments), (b) any custom Python `LlmBackend`, or (c) external OpenAI-compatible APIs / `omp` extensions (`OMP_MODEL`). |
| **3. Dynamic Host Tools & Scope Injection** | `src/omp_framework/tools/` | Register Host Tools with `@registry.tool(...)` and Pydantic parameter models. `ScopeContext` (`scope_id`, `as_of`, `tenant`, `read_only`) is injected by the host—never chosen by the LLM—preventing cross-scope hallucination or data leakage. Automatically tracks `SourceRef` provenance and `Citation` paths. |
| **4. PolicyGate & Governance Store** | `src/omp_framework/control/` | Every Host Tool call and Human Action passes through `PolicyGate.authorize()` and writes an immutable `AuditEntry`. Agents can **propose** Intents, Tickets, Fact-Checked Report Drafts, and Simulated Rule Drafts, but **only authorized human roles** (or pre-approved Autonomous Catalog entries) can execute/publish/sign them. |
| **5. Event Trigger Bus & Expert Memory** | `src/omp_framework/triggers/` & `memory/` | Deduplicates external/cron anomaly events by `(scope_id, event_id)` and wakes up the designated `omp` agent. `ExpertMemoryStore` retains and recalls human-adjudicated decision criteria across sessions. |
| **6. Gated 8-Step Onboarding Wizard** | `src/omp_framework/onboarding/` | Built-in **Requirement Discovery Interview (Step 1)** + sequential verification gate (Steps 2–8). Locks Step $N+1$ until Step $N$ passes live automated verification. Available in Web UI, CLI, and AI Agent Skill. |
| **7. Launchpad UI & REST API** | `src/omp_framework/api/` & `apps/launchpad/` | Ready-to-use FastAPI backend and interactive dark-mode Launchpad UI with Setup Wizard, Agent Console, Tool Trace & Citations, Human Approval Queues, Rule Simulation Diffs, Event Trigger Bus, and Audit Trail. |

---

## 🚀 Quick Start

```powershell
# 1. Install dependencies (including FastAPI & uvicorn) + fetch omp 18.11.2 + launch server
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

Then open **`http://127.0.0.1:8710`** in your browser.

### Manual Commands

```powershell
# Sync virtual environment
uv sync --extra api

# Download & verify omp 18.11.2 (SHA-256 checked, stored in gitignored tools/omp/omp.exe)
powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1

# Run the test suite
.venv\Scripts\python -m pytest -q

# Run smoke check against running server
powershell -ExecutionPolicy Bypass -File scripts/smoke.ps1
```

---

## 🧩 The 4 Universal Agent Archetypes Included

In `agents/omp/*.md` and `src/starter_domain/pack.py`, the blueprint ships with **4 reusable agent archetypes** that fit almost any domain:

1. **`query` (`agents/omp/query.md`) — Read-Only Diagnostic Analyst**
   - Calls read-only telemetry/anomaly tools and answers questions with verifiable `SourceRef` citations.
2. **`report` (`agents/omp/report.md`) — Closed-Loop Fact-Checked Writer**
   - Reads metrics and calls `submit_report_draft`. The host deterministically verifies every claimed number against the session's tool outputs before allowing human publication (`publish_report`).
3. **`rulecard` (`agents/omp/rulecard.md`) — Natural-Language-to-Policy Compiler**
   - Reads current thresholds, runs a historical backtest (`simulate_rule_change`), verifies safety-floor compliance, and proposes a versioned `RuleDraft` for human expert signature (`sign_rule_draft`).
4. **`ops` (`agents/omp/ops.md`) — Event-Driven Triage & Action Proposer**
   - Recalls long-term expert criteria (`recall_expert_criteria`), inspects anomalies, opens deduplicated tickets (`create_ticket`), and proposes control-plane intents (`propose_intent`) gated by human approval or the Autonomous Catalog.

---

## 🛠️ How to Use & Customize This Blueprint (Gated 8-Step Workflow)

The framework includes an interactive **8-Step Gated Onboarding & Requirement Discovery Workflow** (`src/omp_framework/onboarding/`) that **asks for your requirements first (Step 1)** and **locks Step $N+1$ until Step $N$ passes automated verification**.

### 3 Ways to Run the Step-by-Step Workflow

1. **Interactive Web Launchpad Wizard (Recommended)**:
   - Start the server with `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1` and open **`http://127.0.0.1:8710`**.
   - On Tab **`🧭 0. Step-by-Step Setup Wizard`**, fill out **`📋 Step 1: Requirement Discovery Questionnaire`** (11 questions covering your project name, domain purpose, `ScopeContext` entity, metrics, agent archetypes, tools, human approver roles, triggers, and LLM mode) and click **`💾 Save Requirements & Complete Step 1`**.
   - Follow Steps 2 → 8 in order, clicking **`✓ Verify Step N & Unlock Next`** as you complete each step.

2. **Interactive Terminal CLI Wizard**:
   ```powershell
   # Step 1: Run the interactive 11-question Requirement Discovery Interview
   # (Generates var/domain_requirements.json & var/DOMAIN_BLUEPRINT_SPEC.md and unlocks Step 2)
   .venv\Scripts\python -m omp_framework.onboarding interview

   # Optional: Auto-generate a ready-to-run custom Domain Pack & agents/omp/*.md from Step 1 answers!
   .venv\Scripts\python -m omp_framework.onboarding scaffold

   # Check current active step, files to edit, and instructions
   .venv\Scripts\python -m omp_framework.onboarding status

   # Verify the current step and unlock the next step
   .venv\Scripts\python -m omp_framework.onboarding verify

   # Or verify all 8 steps sequentially
   .venv\Scripts\python -m omp_framework.onboarding verify-all

   # Reset progress back to Step 1
   .venv\Scripts\python -m omp_framework.onboarding reset
   ```

3. **With AI Coding Agents (VS Code Copilot / Cursor / Claude)**:
   - The repo includes [`.github/skills/blueprint-onboarding/SKILL.md`](.github/skills/blueprint-onboarding/SKILL.md). Simply ask your AI assistant: *"Help me customize this framework for my domain"* and it will interview you for the 11 requirements first, then walk you through Steps 2 → 8 one verified step at a time.

### The 8 Gated Steps at a Glance

| Step | Step ID | What You Do | Automated Gate Check |
|---|---|---|---|
| **Step 1** | `step-1-requirements` | Answer the 11 Requirement Discovery questions (UI form or CLI `interview`) | Validates `DomainRequirementSpec` & generates `var/DOMAIN_BLUEPRINT_SPEC.md` |
| **Step 2** | `step-2-environment` | Run `uv sync --extra api` & `scripts/get-omp.ps1` | Verifies `omp.exe` SHA-256, isolated `var/omp-home/`, and rendered `models.yml` |
| **Step 3** | `step-3-domain-scopes` | Define target scopes & telemetry in `src/starter_domain/pack.py` | Verifies `pack.scopes` schema (`scope_id`, `name`, `metrics`) |
| **Step 4** | `step-4-host-tools` | Register `@registry.tool(...)` schemas & handlers in `src/starter_domain/pack.py` | Verifies read-only + mutating tools exist and **no `scope_id` leaks into LLM arguments** |
| **Step 5** | `step-5-agent-profiles` | Customize `agents/omp/*.md` profiles (`tools`, `max_tool_calls`, `timeout_s`) | Verifies `omp-agent:` markers and checks that every declared tool exists in `ToolRegistry` |
| **Step 6** | `step-6-policy-gate` | Configure human roles (`gate.py`) & `CatalogEntry` items (`pack.py`) | Runs live `PolicyGate.authorize()` checks confirming agents are blocked from human actions |
| **Step 7** | `step-7-llm-planner` | Configure `OfflinePlanner` (`pack.py`) or external LLM (`models.yml` / `OMP_MODEL`) | Runs completion check across every loaded agent profile |
| **Step 8** | `step-8-e2e-run` | Run end-to-end governed agent execution & human sign-off | Executes a governed `ToolSession` call and verifies `SourceRef` citations + `AuditSink` entries |

For code snippets and deep-dive examples, see **[docs/CUSTOMIZATION_GUIDE.md](docs/CUSTOMIZATION_GUIDE.md)**.

---

## 📁 Repository Structure

```text
OmpAgentFramework/
├── .github/
│   └── skills/
│       └── blueprint-onboarding/
│           └── SKILL.md             # AI Agent Skill: interviews requirements first, then steps 1->8
├── AGENTS.md                        # AI engineering & architecture contract
├── README.md                        # Quickstart & architecture overview
├── pyproject.toml                   # Python 3.12 package & pytest configuration
├── agents/
│   └── omp/
│       ├── config.yml               # Runner model & template configuration
│       ├── overlay.yml              # omp --config security overlay (disables built-in discovery/tools)
│       ├── query.md                 # Archetype 1: Read-only Q&A analyst
│       ├── report.md                # Archetype 2: Fact-checked report writer
│       ├── rulecard.md              # Archetype 3: Simulated policy/rule compiler
│       ├── ops.md                   # Archetype 4: Event triage, tickets, intents & memory
│       └── agent/
│           └── models.yml           # Template rendered to var/omp-home/agent/models.yml
├── apps/
│   └── launchpad/
│       └── index.html               # Interactive Governance & Agent Launchpad UI (with Setup Wizard)
├── docs/
│   └── CUSTOMIZATION_GUIDE.md       # Step-by-step blueprint customization guide
├── scripts/
│   ├── get-omp.ps1                  # Downloads & SHA-256 verifies omp 18.11.2
│   ├── dev.ps1                      # One-command startup script
│   └── smoke.ps1                    # Endpoint smoke test script
├── src/
│   ├── omp_framework/               # REUSABLE FRAMEWORK CORE (Domain-Agnostic)
│   │   ├── contracts.py             # ScopeContext, Citations, Gate, Proposals, LLM schemas
│   │   ├── onboarding/              # Gated 8-step Requirement Interview & Verification Wizard
│   │   ├── rpc/                     # omp stdio NDJSON v2 client & sandboxed process manager
│   │   ├── llm/                     # /llm/v1 SSE proxy & OfflinePlanner base
│   │   ├── tools/                   # Dynamic @registry.tool decorator & ScopeContext injection
│   │   ├── control/                 # PolicyGate RBAC, AuditSink & GovernanceStore
│   │   ├── triggers/                # Deduplicated EventTriggerBus -> wakes omp agent
│   │   ├── memory/                  # ExpertMemoryStore for long-term human criteria
│   │   ├── runner/                  # AgentRunner, prompt security guards & .md loader
│   │   └── api/                     # FastAPI server factory
│   └── starter_domain/              # REPLACEABLE STARTER DOMAIN PACK
│       └── pack.py                  # Sample domain data, 9 Host Tools & offline planners
└── tests/
    ├── fake_omp.py                  # Protocol v2 omp test double
    └── test_framework.py            # End-to-end unit & integration test suite
```
