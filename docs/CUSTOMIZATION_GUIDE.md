# Blueprint Customization Guide: Building Your Own System with `OmpAgentFramework`

This guide explains how to take the `OmpAgentFramework` blueprint and adapt it to **any domain** (e.g., FinOps cost governance, Cybersecurity SOC triage, Smart Factory PLC monitoring, Hospital ICU alerting, or Civil Infrastructure monitoring) without touching the core `omp` RPC or governance engine.

---

## 1. Architectural Separation: What Stays vs. What You Customize

| Layer | Path | Action When Creating a New System |
|---|---|---|
| **Framework Core** | `src/omp_framework/` | **Keep as-is.** Handles `omp` child-process sandboxing, NDJSON v2 RPC, `/llm/v1` SSE proxy, `ScopeContext` injection, `PolicyGate`, `AuditSink`, and REST routes. |
| **Domain Pack** | `src/starter_domain/` | **Customize or replace.** Define your domain data sources, Pydantic tool argument schemas, `@registry.tool` handlers, fact-check rules, and catalog entries. |
| **Agent Profiles** | `agents/omp/*.md` | **Customize or add files.** Each `.md` file defines one agent's allowed Host Tools, `max_tool_calls`, `timeout_s`, and system prompt. |
| **LLM Routing** | `agents/omp/agent/models.yml` | **Configure as needed.** Use the built-in deterministic `OfflinePlanner` for testing/fallback, or point to an OpenAI-compatible LLM / `omp` extension. |

---

## 2. Step-by-Step: Adding a New Host Tool

Open `src/starter_domain/pack.py` (or create your own domain pack module).

### Step 2.1: Define the Tool's Pydantic Argument Schema

**Important Security Rule:** Never put `scope_id`, `tenant`, or `as_of` in the LLM's tool arguments! The framework automatically injects `inv.scope` (`ScopeContext`) from the host so the LLM can never query or mutate the wrong target scope.

```python
from pydantic import Field
from omp_framework.contracts import ContractModel

class InspectDatabaseArgs(ContractModel):
    table_name: str = Field(min_length=1, description="Database table to inspect")
    include_slow_queries: bool = Field(default=True, description="Include slow query log summary")
```

### Step 2.2: Register the Tool with `@registry.tool`

Choose the appropriate governance `rule` for the `PolicyGate`:
- `"read_only"`: Allowed in all modes (including `ScopeContext.read_only=True`).
- `"report_draft"`: Submits a draft for deterministic fact-checking; requires human `publish_report` to publish.
- `"rule_draft"`: Proposes a versioned policy/threshold change; requires human `sign_rule_draft` to activate.
- `"ticket"`: Creates a deduplicated incident ticket (checked against `CatalogEntry`).
- `"intent"`: Proposes a control-plane action; queued as `pending_approval` for human review unless `autonomous_enabled` + catalog match.
- `"expert_memory"`: Retains a human-adjudicated decision pattern into `ExpertMemoryStore`.

```python
from omp_framework.contracts import SourceRef
from omp_framework.tools import ToolInvocation, ToolOutput

@self.registry.tool(
    "inspect_database",
    label="Inspect Database Health",
    description="Read table statistics and slow queries for the active scope.",
    domain="database",
    rule="read_only",
    args_model=InspectDatabaseArgs,
)
def inspect_database(inv: ToolInvocation) -> ToolOutput:
    args: InspectDatabaseArgs = inv.args
    scope_id = inv.scope.scope_id  # <-- Host-injected scope!
    return ToolOutput(
        data={
            "scope_id": scope_id,
            "table": args.table_name,
            "row_count": 142000,
            "slow_queries_5m": 3 if args.include_slow_queries else 0,
        },
        sources=[
            ("slow_queries_5m", SourceRef(layer="db.telemetry", field="slow_queries_5m", target=args.table_name)),
        ],
    )
```

---

## 3. Step-by-Step: Adding or Customizing an `omp` Agent Profile

Create a new file `agents/omp/<agent_id>.md`. The `AgentRunner` automatically discovers all `*.md` files in `agents/omp/` at startup and registers their allowed tools with `PolicyGate`.

```markdown
omp-agent: db_advisor
label: Database Performance Advisor
domain: database
description: Diagnoses slow queries and proposes index or connection-pool intents.
tools:
  - inspect_database
  - propose_intent
max_tool_calls: 8
timeout_s: 90
---
You are the **Database Performance Advisor (`db_advisor`)**.

1. Always call `inspect_database` first to gather verified metrics.
2. Cite the exact `slow_queries_5m` count in your response.
3. If `slow_queries_5m > 0`, call `propose_intent` to suggest a connection pool adjustment for human approval.
```

---

## 4. Connecting a Real LLM (3 Supported Modes)

### Mode 1: Deterministic Offline Planner (Default)
Out of the box, `omp` sends OpenAI-compatible requests to `http://127.0.0.1:8710/llm/v1`, which routes to `OfflinePlanner`. This lets you run the full `omp` binary, RPC loop, PolicyGate, and UI **offline with zero API keys**.

### Mode 2: Custom Python `LlmBackend` via `/llm/v1`
Implement the `LlmBackend` protocol (`mode: str` and `complete(request: ChatCompletionRequest) -> ChatCompletionResponse`) in Python and set:
```powershell
$env:OMP_LLM_BACKEND = "my_domain.llm:OpenAiBackend"
```

### Mode 3: Direct External OpenAI-Compatible Provider in `models.yml`
Edit `agents/omp/agent/models.yml` to add your provider and set `$env:OMP_MODEL`:
```yaml
providers:
  ext:
    baseUrl: "https://api.openai.com/v1"
    apiKey: "sk-..."
    api: openai-completions
    models:
      - id: gpt-4o
        name: GPT-4o
        reasoning: false
        input: [text]
        contextWindow: 128000
        maxTokens: 4096
```
Then run with:
```powershell
$env:OMP_MODEL = "ext/gpt-4o"
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

---

## 5. Customizing Human Roles & Autonomous Catalog

In `src/omp_framework/control/gate.py` (`DEFAULT_HUMAN_ACTIONS`) and `src/starter_domain/pack.py` (`DEFAULT_CATALOG`):

- **Human Actions (`HumanActionSpec`)**: Define which human roles (`ops_lead`, `domain_expert`, `safety_officer`, `admin`) are permitted to approve intents, sign rule drafts, or publish reports.
- **Pre-Approved Catalog (`CatalogEntry`)**: Define which `ticket:<category>` or `intent:<kind>` actions are recognized by the PolicyGate, and whether they can execute automatically when `autonomous_enabled` is toggled ON.
