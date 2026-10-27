"""Bounds on what one request, and one user, can make the LLM generate."""

import importlib
import os

import pytest
from fastapi.testclient import TestClient

import agent
import auth
import graph
import llm
import quota
import tools


def _tool_turn(i, tokens=0):
    return llm.LLMResponse(
        content=[{"type": "tool_use", "id": f"t{i}", "name": "find_symbol",
                  "input": {"query": f"q{i}"}}],
        stop_reason="tool_use", input_tokens=tokens, output_tokens=0)


@pytest.fixture
def endless_model(monkeypatch):
    """A model that never stops asking for tools — the runaway case."""
    calls = []

    def fake(*, system, messages, tools, model, max_tokens=2048):
        calls.append({"model": model, "max_tokens": max_tokens, "tools": bool(tools)})
        if not tools:  # forced synthesis
            return llm.LLMResponse([{"type": "text", "text": "final"}], "end_turn", 10, 10)
        return _tool_turn(len(calls), tokens=60_000)

    monkeypatch.setattr(llm, "create_message", fake)
    monkeypatch.setattr(tools, "run_tool", lambda n, i: [])
    return calls


def test_token_budget_stops_the_loop(endless_model, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_BUDGET", "100000")
    out = agent.ask("q", max_steps=20)
    nav = [c for c in endless_model if c["tools"]]
    assert len(nav) == 2  # 60k + 60k crosses 100k: no third navigation call
    assert out["stopped"] == "token budget"
    final = endless_model[-1]
    assert not final["tools"] and final["max_tokens"] == 1024
    assert final["model"] == llm.NAV_MODEL  # the expensive model is for the step cap only
    assert out["usage"]["input_tokens"] == 120_010


def test_time_limit_stops_without_another_call(endless_model, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEOUT_S", "0")
    out = agent.ask("q", max_steps=20)
    assert endless_model == []  # deadline already passed: zero model calls
    assert out["stopped"] == "time limit" and "time limit" in out["answer"]


def test_step_cap_still_ends_in_one_synthesis(endless_model, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_BUDGET", "10000000")
    out = agent.ask("q", max_steps=3)
    assert [c["tools"] for c in endless_model] == [True, True, True, False]
    assert endless_model[-1]["model"] == llm.SYNTH_MODEL
    assert out["stopped"] is None


def _client(monkeypatch, **env):
    for k in ("AUTH_MODE", "API_TOKEN", "ALB_ARN", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main

    return TestClient(importlib.reload(main).app)


def test_overlong_question_is_rejected_before_any_work(monkeypatch):
    called = []
    monkeypatch.setattr(agent, "ask", lambda *a, **k: called.append(1) or {})
    c = _client(monkeypatch, MAX_QUESTION_CHARS="100", DAILY_TOKEN_QUOTA="0")
    assert c.post("/ask", json={"question": "x" * 101}).status_code == 422
    assert c.post("/impact/r1/ask", json={"question": "x" * 101}).status_code == 422
    assert called == []


def test_daily_quota_blocks_and_charges(monkeypatch):
    """Store faked: the wiring — refuse when spent, charge actual usage."""
    auth._calls.clear()
    spent = {}
    monkeypatch.setattr(quota, "used", lambda user: spent.get(user, 0))
    monkeypatch.setattr(graph, "_run", lambda q, **p: spent.__setitem__(
        p["user"], spent.get(p["user"], 0) + p["tokens"]) or [])
    monkeypatch.setattr(graph, "get_profile", lambda: {})
    monkeypatch.setattr(agent, "ask", lambda q, **k: {
        "answer": "ok", "usage": {"input_tokens": 700, "output_tokens": 300}})
    c = _client(monkeypatch, DAILY_TOKEN_QUOTA="1500")
    assert c.post("/ask", json={"question": "q"}).status_code == 200  # 1000 used
    assert c.post("/ask", json={"question": "q"}).status_code == 200  # 2000: overshoot by one
    r = c.post("/ask", json={"question": "q"})
    assert r.status_code == 429 and "quota" in r.json()["detail"]
    assert spent == {"local": 2000}
    auth._calls.clear()


URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


@pytest.mark.skipif(not (URI and PASSWORD), reason="no Neo4j at NEO4J_TEST_URI")
def test_quota_persists_in_the_graph(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("NEO4J_URI", URI)
    monkeypatch.setenv("NEO4J_PASSWORD", PASSWORD)
    monkeypatch.setenv("DAILY_TOKEN_QUOTA", "1000")
    graph.close_driver()
    graph.setup_schema()
    graph._run("MATCH (q:UsageQuota) DELETE q")
    req = SimpleNamespace(state=SimpleNamespace(user={"sub": "alice"}), client=None)
    try:
        quota.charge(req, {"input_tokens": 600, "output_tokens": 0})
        quota.charge(req, {"input_tokens": 500, "output_tokens": 0})
        assert quota.used("alice") == 1100  # survives process state: read from Neo4j
        with pytest.raises(Exception) as exc:
            quota.guard(req)
        assert exc.value.status_code == 429
        assert quota.used("bob") == 0  # per user
    finally:
        graph._run("MATCH (q:UsageQuota) DELETE q")
        graph.close_driver()
