"""Fake omp RPC executable for fast unit/integration tests without requiring a live omp binary or LLM server.

Speaks the exact omp 18.11.2 NDJSON over stdio protocol v2 (`ready`, `negotiate_protocol`, `set_host_tools`,
`get_state`, `prompt`, `host_tool_call` / `host_tool_result`, `get_last_assistant_text`, `abort`).
Can also call a live `/llm/v1/chat/completions` endpoint if `FAKE_OMP_USE_LLM_URL` is set, or execute
planned tool sequences based on the agent marker in `--system-prompt`.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any


def _send(frame: dict[str, Any]) -> None:
    data = json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _read_frame() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    text = line.decode("utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _call_host_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call_id = f"htc-{uuid.uuid4().hex[:8]}"
    tc_id = f"call_{uuid.uuid4().hex[:8]}"
    _send(
        {
            "type": "host_tool_call",
            "id": call_id,
            "toolCallId": tc_id,
            "toolName": tool_name,
            "arguments": arguments,
        }
    )
    while True:
        frame = _read_frame()
        if frame is None:
            return {"error": "eof"}
        if frame.get("type") == "host_tool_result" and frame.get("id") == call_id:
            res = frame.get("result") or {}
            content = res.get("content") or []
            text = content[0].get("text", "{}") if content else "{}"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}


def _run_agent_workflow(agent_id: str, declared_tools: list[str]) -> str:
    if agent_id == "query":
        m = _call_host_tool("get_system_metrics", {}) if "get_system_metrics" in declared_tools else {}
        a = _call_host_tool("list_recent_anomalies", {}) if "list_recent_anomalies" in declared_tools else {}
        metrics = m.get("metrics", {})
        anomalies = a.get("anomalies", [])
        return (
            f"### Scope Diagnostic Summary (`{m.get('scope_id')}` as of `{m.get('as_of')}`)\n"
            f"- **Overall Status**: `{m.get('overall_status')}` (Grade {m.get('overall_grade')})\n"
            f"- **Availability SLA**: `{m.get('availability_sla')}`\n"
            f"- **Metrics**: CPU Load Ratio = `{metrics.get('cpu_load_ratio')}`, "
            f"P99 Latency = `{metrics.get('p99_latency_ms')} ms`\n"
            f"- **Active Anomalies ({len(anomalies)})**: "
            + (", ".join(x["event_id"] for x in anomalies) if anomalies else "None")
        )

    if agent_id == "report":
        m = _call_host_tool("get_system_metrics", {})
        metrics = m.get("metrics", {})
        if "submit_report_draft" in declared_tools:
            r = _call_host_tool(
                "submit_report_draft",
                {
                    "title": f"Operational Health & SLA Assessment — {m.get('scope_id')}",
                    "summary": f"Verified telemetry report for {m.get('scope_id')} with CPU {metrics.get('cpu_load_ratio')}.",
                    "claimed_cpu_load_ratio": float(metrics.get("cpu_load_ratio", 0.0)),
                    "claimed_p99_latency_ms": float(metrics.get("p99_latency_ms", 0.0)),
                    "claimed_availability_sla": float(m.get("availability_sla", 0.0)),
                    "recommendations": ["Investigate link freeze on node-api-01", "Approve traffic reroute intent"],
                },
            )
            return (
                f"Fact-Checked Report Draft `{r.get('report_id')}` created "
                f"(passed_factcheck={r.get('passed_factcheck')}, rounds={r.get('rounds_used')})."
            )
        return "Read-only mode active: skipped report draft submission."

    if agent_id == "rulecard":
        _call_host_tool("get_active_rule", {})
        sim = _call_host_tool(
            "simulate_rule_change",
            {"yellow_threshold": 0.65, "orange_threshold": 0.80, "red_threshold": 0.92, "observation_window_minutes": 15},
        )
        if "propose_rule_draft" in declared_tools:
            dr = _call_host_tool(
                "propose_rule_draft",
                {
                    "yellow_threshold": 0.65,
                    "orange_threshold": 0.80,
                    "red_threshold": 0.92,
                    "observation_window_minutes": 15,
                    "notify_roles": ["oncall_sre", "ops_lead"],
                    "rationale": "Tighten thresholds to catch latency spikes earlier.",
                },
            )
            return (
                f"Rule Draft `{dr.get('draft_id')}` proposed ({dr.get('direction')}), "
                f"alert delta={sim.get('delta_alerts')}."
            )
        return "Simulated rule thresholds in read-only mode."

    if agent_id == "ops":
        _call_host_tool("recall_expert_criteria", {})
        anom = _call_host_tool("list_recent_anomalies", {})
        anomalies = anom.get("anomalies") or []
        evt_id = anomalies[0]["event_id"] if anomalies else "EVT-20261031-01"
        node_id = anomalies[0]["node_id"] if anomalies else "node-api-01"
        tkt_id = None
        int_id = None
        if "create_ticket" in declared_tools:
            tkt = _call_host_tool(
                "create_ticket",
                {
                    "event_id": evt_id,
                    "category": "network",
                    "severity": "high",
                    "title": f"Gateway link interruption on {node_id}",
                    "summary": f"Cross-ID and expert memory classify {evt_id} as link freeze.",
                },
            )
            tkt_id = tkt.get("ticket_id")
        if "propose_intent" in declared_tools:
            intent = _call_host_tool(
                "propose_intent",
                {
                    "kind": "reroute_traffic",
                    "target": node_id,
                    "trigger_event": evt_id,
                    "rationale": f"Drain traffic from {node_id} due to {evt_id}.",
                    "parameters": {"backup_node": "node-api-02"},
                },
            )
            int_id = intent.get("intent_id")
        return f"Ops triage complete for `{evt_id}`: ticket=`{tkt_id}`, intent=`{int_id}`."

    if declared_tools:
        res = _call_host_tool(declared_tools[0], {})
        return f"Executed `{declared_tools[0]}`: {json.dumps(res)}"
    return "No tools declared."


def main() -> int:
    args = sys.argv[1:]
    model = "omp/omp-offline"
    system_prompt_path: str | None = None
    session_dir: str | None = None
    for idx, arg in enumerate(args):
        if arg == "--model" and idx + 1 < len(args):
            model = args[idx + 1]
        elif arg == "--system-prompt" and idx + 1 < len(args):
            system_prompt_path = args[idx + 1]
        elif arg == "--session-dir" and idx + 1 < len(args):
            session_dir = args[idx + 1]

    sys_prompt = ""
    agent_id = "query"
    if system_prompt_path and Path(system_prompt_path).is_file():
        sys_prompt = Path(system_prompt_path).read_text(encoding="utf-8")
        for line in sys_prompt.splitlines():
            if line.strip().startswith("omp-agent:"):
                agent_id = line.split(":", 1)[1].strip()
                break

    _send(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": 1048576,
            "maxReassembledFrameBytes": 67108864,
        }
    )

    declared_tools: list[str] = []
    last_text = ""
    session_id = f"sess-{uuid.uuid4().hex[:8]}"

    while True:
        frame = _read_frame()
        if frame is None:
            break
        if not frame:
            continue
        cmd = frame.get("type")
        req_id = frame.get("id")

        if cmd == "negotiate_protocol":
            _send({"type": "response", "id": req_id, "command": cmd, "success": True, "data": {"protocolVersion": 2}})
        elif cmd == "set_host_tools":
            tools = frame.get("tools") or []
            declared_tools = [t["name"] for t in tools if isinstance(t, dict) and "name" in t]
            _send({"type": "response", "id": req_id, "command": cmd, "success": True, "data": {"toolNames": declared_tools}})
        elif cmd == "get_state":
            _send(
                {
                    "type": "response",
                    "id": req_id,
                    "command": cmd,
                    "success": True,
                    "data": {
                        "sessionId": session_id,
                        "sessionFile": str(Path(session_dir) / f"{session_id}.jsonl") if session_dir else None,
                        "model": model,
                        "systemPrompt": sys_prompt,
                        "isSettled": True,
                    },
                }
            )
        elif cmd == "prompt":
            _send({"type": "response", "id": req_id, "command": cmd, "success": True, "data": {"agentInvoked": True}})
            _send({"type": "agent_start"})
            last_text = _run_agent_workflow(agent_id, declared_tools)
            _send({"type": "agent_end"})
            _send(
                {
                    "type": "prompt_result",
                    "id": req_id,
                    "agentInvoked": True,
                    "status": "completed",
                    "sessionSettled": True,
                }
            )
            _send({"type": "session_settled"})
        elif cmd == "get_last_assistant_text":
            _send({"type": "response", "id": req_id, "command": cmd, "success": True, "data": {"text": last_text}})
        elif cmd == "abort":
            _send({"type": "response", "id": req_id, "command": cmd, "success": True, "data": {}})
        else:
            _send({"type": "response", "id": req_id, "command": str(cmd), "success": False, "error": f"Unknown command {cmd}"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
