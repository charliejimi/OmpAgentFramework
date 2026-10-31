"""Comprehensive test suite for OmpAgentFramework (RPC, PolicyGate, Tools, Governance, Triggers, API, and real OMP)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omp_framework.api.app import create_app
from omp_framework.contracts import (
    Action,
    ChatCompletionRequest,
    ChatMessage,
    GateContext,
    Principal,
    ScopeContext,
)
from omp_framework.control.gate import AuditSink, PolicyGate
from omp_framework.control.store import GovernanceStore
from omp_framework.rpc.client import FrameDecoder, RpcProtocolError
from omp_framework.rpc.process import (
    DEFAULT_OMP_EXE,
    OmpConfigError,
    OmpPaths,
    isolated_env,
)
from omp_framework.runner.runner import (
    is_command_prompt,
    is_file_mention_prompt,
    load_all_agent_profiles,
)
from omp_framework.tools.registry import ToolRegistry
from starter_domain.pack import build_starter_pack

FAKE_OMP_CMD = (sys.executable, str(Path(__file__).parent / "fake_omp.py"))


def test_frame_decoder_v2_chunk_reassembly() -> None:
    decoder = FrameDecoder(accept_chunks=True)
    payload = {"type": "host_tool_call", "id": "1", "toolName": "ping", "arguments": {"msg": "你好世界🌍" * 50}}
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    mid = len(raw_bytes) // 2
    part0 = base64.b64encode(raw_bytes[:mid]).decode("ascii")
    part1 = base64.b64encode(raw_bytes[mid:]).decode("ascii")

    c0 = {"type": "rpc_chunk", "chunkId": "chk-1", "index": 0, "count": 2, "byteLength": len(raw_bytes), "data": part0}
    c1 = {"type": "rpc_chunk", "chunkId": "chk-1", "index": 1, "count": 2, "byteLength": len(raw_bytes), "data": part1}

    assert decoder.push(c0) is None
    assert decoder.pending is True
    reassembled = decoder.push(c1)
    assert decoder.pending is False
    assert reassembled == payload
    assert decoder.reassembled == 1


def test_frame_decoder_rejects_unnegotiated_chunks() -> None:
    decoder = FrameDecoder(accept_chunks=False)
    with pytest.raises(RpcProtocolError):
        decoder.push({"type": "rpc_chunk", "chunkId": "c1", "index": 0, "count": 1, "byteLength": 2, "data": "e30="})


def test_isolated_env_strips_secrets_and_sets_no_proxy(tmp_path: Path) -> None:
    paths = OmpPaths.under(tmp_path / "var")
    paths.prepare()
    base_env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp-secret",
        "DB_PASSWORD": "pw",
        "AWS_SECRET_ACCESS_KEY": "aws-sec",
        "HTTP_PROXY": "http://proxy.corp:8080",
        "no_proxy": "internal.corp",
    }
    env = isolated_env(paths, base=base_env, provider_env={"CURSOR_API_KEY": "cur-whitelisted"})
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["CURSOR_API_KEY"] == "cur-whitelisted"
    assert env["USERPROFILE"] == str(paths.home)
    assert env["PI_CODING_AGENT_DIR"] == str(paths.agent)
    assert "127.0.0.1" in env["NO_PROXY"]
    assert "localhost" in env["NO_PROXY"]
    assert "internal.corp" in env["NO_PROXY"]

    with pytest.raises(OmpConfigError):
        isolated_env(paths, base={}, extra={"MY_SECRET_KEY": "nope"})


def test_prompt_security_guards() -> None:
    assert is_command_prompt("/export C:/secret.json") is True
    assert is_command_prompt("  \ufeff /help") is True
    assert is_command_prompt("Normal question about /api/v1/health endpoint") is False

    assert is_file_mention_prompt("Please read @/etc/passwd") is True
    assert is_file_mention_prompt("Check @~/.ssh/id_rsa") is True
    assert is_file_mention_prompt("Contact ops@example.com or node@2026") is False


def test_policy_gate_rbac_and_tool_boundaries() -> None:
    audit = AuditSink()
    registry = ToolRegistry()
    gate = PolicyGate(audit=audit, tool_registry=registry)
    store = GovernanceStore(gate=gate)
    build_starter_pack(registry=registry, gate=gate, store=store)

    profiles = load_all_agent_profiles()
    for aid, prof in profiles.items():
        gate.register_agent_tools(aid, prof.tools)

    ctx = GateContext(scope=ScopeContext(scope_id="prod-cluster-01"))

    # 1. Agent cannot execute human action
    d_agent_human = gate.authorize(Principal.for_agent("ops"), Action.human("approve_intent"), ctx)
    assert d_agent_human.effect == "deny"
    assert d_agent_human.basis == "agent_human_action"

    # 2. Human cannot execute Host Tool
    d_human_tool = gate.authorize(Principal.for_human("alice", "ops_lead"), Action.tool("get_system_metrics"), ctx)
    assert d_human_tool.effect == "deny"

    # 3. Query agent cannot call mutating tool create_ticket
    d_query_write = gate.authorize(Principal.for_agent("query"), Action.tool("create_ticket"), ctx)
    assert d_query_write.effect == "deny"
    assert d_query_write.basis == "tool_not_allowed"

    # 4. Read-only ScopeContext blocks mutating tools even for ops agent
    ro_ctx = GateContext(scope=ScopeContext(scope_id="prod-cluster-01", read_only=True), arguments={"category": "network"})
    d_ro = gate.authorize(Principal.for_agent("ops"), Action.tool("create_ticket"), ro_ctx)
    assert d_ro.effect == "deny"
    assert d_ro.basis == "tool_not_allowed"

    assert len(audit.list_entries()) == 4


def test_api_and_4_agent_archetypes_with_fake_omp(tmp_path: Path) -> None:
    paths = OmpPaths.under(tmp_path / "var")
    app = create_app(port=8799, omp_command=FAKE_OMP_CMD, omp_paths=paths)
    client = TestClient(app)

    # Health & Launchpad
    r_health = client.get("/api/v1/health")
    assert r_health.status_code == 200
    assert r_health.json()["data"]["domain"] == "service-reliability-sentinel"

    # Prompt guards return HTTP 400
    assert client.post("/api/v1/agents/query/ask", json={"prompt": "/export foo"}).status_code == 400
    assert client.post("/api/v1/agents/query/ask", json={"prompt": "Read @../secret.txt"}).status_code == 400

    # 1. Query Agent
    r_q = client.post("/api/v1/agents/query/ask", json={"prompt": "Check status", "scope_id": "prod-cluster-01"})
    assert r_q.status_code == 200
    q_data = r_q.json()["data"]
    assert q_data["status"] == "ok"
    assert len(q_data["tool_calls"]) == 2
    assert len(q_data["citations"]) >= 3

    # 2. Report Agent (Fact-checked draft -> Human publish)
    r_rep = client.post("/api/v1/agents/report/ask", json={"prompt": "Draft SLA report", "scope_id": "prod-cluster-01"})
    assert r_rep.status_code == 200
    reports = client.get("/api/v1/reports?scope_id=prod-cluster-01").json()["data"]
    assert len(reports) == 1
    assert reports[0]["passed_factcheck"] is True
    assert reports[0]["status"] == "draft_verified"

    # Unauthorized role cannot publish report
    r_pub_bad = client.post(f"/api/v1/reports/{reports[0]['report_id']}/publish", json={"user_id": "bob", "role": "viewer"})
    assert r_pub_bad.status_code == 403

    # Authorized domain_expert publishes report
    r_pub_ok = client.post(
        f"/api/v1/reports/{reports[0]['report_id']}/publish",
        json={"user_id": "dr-chen", "role": "domain_expert"},
    )
    assert r_pub_ok.status_code == 200
    assert r_pub_ok.json()["data"]["status"] == "published"

    # 3. Rulecard Agent (Simulate -> Propose Draft -> Human Sign)
    r_rc = client.post("/api/v1/agents/rulecard/ask", json={"prompt": "Tighten rules", "scope_id": "prod-cluster-01"})
    assert r_rc.status_code == 200
    rules_info = client.get("/api/v1/rules?scope_id=prod-cluster-01").json()["data"]
    assert len(rules_info["drafts"]) == 1
    draft_id = rules_info["drafts"][0]["draft_id"]
    assert rules_info["active_rule"]["version"] == 1

    r_sign = client.post(f"/api/v1/rules/{draft_id}/sign", json={"user_id": "dr-chen", "role": "domain_expert"})
    assert r_sign.status_code == 200
    assert r_sign.json()["data"]["status"] == "signed_active"
    assert client.get("/api/v1/rules?scope_id=prod-cluster-01").json()["data"]["active_rule"]["version"] == 2

    # 4. Event Trigger Bus -> Wakes Ops Agent -> Creates Deduplicated Ticket & Pending Intent
    r_trig1 = client.post("/api/v1/triggers/emit", json={"event_id": "EVT-20261031-01", "scope_id": "prod-cluster-01"})
    assert r_trig1.status_code == 200
    t1 = r_trig1.json()["data"]
    assert t1["deduplicated"] is False
    assert t1["agent_invoked"] is True
    assert t1["ticket_id"] is not None
    assert t1["intent_id"] is not None

    # Emitting the same event_id again is deduplicated
    r_trig2 = client.post("/api/v1/triggers/emit", json={"event_id": "EVT-20261031-01", "scope_id": "prod-cluster-01"})
    assert r_trig2.json()["data"]["deduplicated"] is True

    # Approve the pending intent as ops_lead
    r_int = client.post(
        f"/api/v1/intents/{t1['intent_id']}/review",
        json={"approve": True, "user_id": "alice-ops", "role": "ops_lead", "review_note": "Approved reroute"},
    )
    assert r_int.status_code == 200
    assert r_int.json()["data"]["status"] == "approved_executed"


@pytest.mark.omp
def test_llm_v1_sse_and_offline_planner_direct(tmp_path: Path) -> None:
    paths = OmpPaths.under(tmp_path / "var")
    app = create_app(port=8798, omp_command=FAKE_OMP_CMD, omp_paths=paths)
    client = TestClient(app)

    req_payload = ChatCompletionRequest(
        model="omp-offline",
        messages=[
            ChatMessage(role="system", content="omp-agent: query\nYou are query agent."),
            ChatMessage(role="user", content="Diagnose cluster"),
        ],
        tools=[{"type": "function", "function": {"name": "get_system_metrics", "parameters": {"type": "object"}}}],
        stream=True,
    ).model_dump()

    res = client.post("/llm/v1/chat/completions", json=req_payload)
    assert res.status_code == 200
    assert "data: [DONE]" in res.text
    assert "get_system_metrics" in res.text
