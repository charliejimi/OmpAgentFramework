# OmpAgentFramework — AI Agent Engineering Contract

This repository is the **General-Purpose Governed Agent Framework Blueprint** powered by `omp` (`oh-my-pi` in RPC mode).

## Architecture Overview

1. **`src/omp_framework/` (Framework Core — Domain-Agnostic)**
   - `contracts.py`: Universal contracts (`ScopeContext`, `SourceRef`, `Citation`, `Principal`, `Action`, `Decision`, `AuditEntry`, `ProposalDraft`, `IntentItem`, `TicketItem`, `RuleDraft`, `ExpertCriterion`, `TriggerEventRecord`).
   - `rpc/`: `omp` stdio NDJSON v2 protocol client (`client.py`) and sandboxed child-process manager (`process.py`) with strict credential stripping, concurrency limiting, and `RunLimits` (`max_tool_calls`, `timeout_s`).
   - `llm/`: Pluggable LLM router (`endpoint.py`, `backend.py`) serving OpenAI-compatible `/llm/v1/chat/completions` (SSE + JSON), supporting:
     1. Deterministic `OfflinePlanner` (for zero-key CI testing and air-gapped fallback),
     2. External OpenAI-compatible providers (`models.yml`),
     3. `omp` extensions (`cursor-sdk`, etc. via `OMP_MODEL`).
   - `tools/`: Dynamic `ToolRegistry` & `@tool_spec` registration (`registry.py`). Host-injected `ScopeContext` prevents LLM scope hallucination or cross-tenant data leakage.
   - `control/`: `PolicyGate` (`gate.py`) enforcing RBAC + Agent-vs-Human boundaries + Autonomous Catalog, plus `ApprovalStore` (`store.py`) for Intents, Tickets, Fact-Checked Report Drafts, and Simulated Rule/Workflow Drafts.
   - `triggers/` & `memory/`: Deduplicated Event Trigger Bus (`triggers/bus.py`) that wakes up designated `omp` agents, and `ExpertMemoryStore` (`memory/store.py`) for retaining human-adjudicated criteria.
   - `runner/`: `AgentRunner` (`runner.py`) reading `agents/omp/<agent>.md`, blocking `/` slash-commands and `@file` mentions, and orchestrating `omp` RPC + Host Tools + Fact-Check loops.
   - `api/`: FastAPI application factory (`app.py`) + Launchpad Governance UI (`apps/launchpad/index.html`).

2. **`src/starter_domain/` (Replaceable Domain Pack Example)**
   - Demonstrates how to build a domain-specific application on top of `omp_framework` using the 4 universal agent archetypes (`query`, `report`, `rulecard`, `ops`).
   - When adopting this blueprint for a new project, replace or extend `src/starter_domain/` and `agents/omp/*.md` without modifying `src/omp_framework/`.

## Commands (Run from Repo Root in PowerShell)

- **Install dependencies**: `uv sync --extra api`
- **Fetch `omp` binary**: `powershell -ExecutionPolicy Bypass -File scripts/get-omp.ps1`
- **Start server & Launchpad UI**: `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1` (`http://127.0.0.1:8710`)
- **Run tests**: `.venv\Scripts\python -m pytest -q`
- **Smoke check**: `powershell -ExecutionPolicy Bypass -File scripts/smoke.ps1`
