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
    monkeypatch.setattr(quota, "limit_for", lambda request: 0)
    c = _client(monkeypatch, MAX_QUESTION_CHARS="100")
    assert c.post("/ask", json={"question": "x" * 101}).status_code == 422
    assert c.post("/impact/r1/ask", json={"question": "x" * 101}).status_code == 422
    assert called == []


def test_daily_quota_blocks_and_charges(monkeypatch):
    """Store faked: the wiring — refuse when spent, charge actual usage."""
    auth._calls.clear()
    spent = {}
    monkeypatch.setattr(quota, "used", lambda user: spent.get(user, 0))
    monkeypatch.setattr(quota, "limit_for", lambda request: quota.daily_limit())
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


@pytest.mark.skipif(not (URI and PASSWORD), reason="no Neo4j at NEO4J_TEST_URI")
def test_per_user_overrides(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("NEO4J_URI", URI)
    monkeypatch.setenv("NEO4J_PASSWORD", PASSWORD)
    monkeypatch.setenv("DAILY_TOKEN_QUOTA", "1000")
    graph.close_driver()
    graph.setup_schema()
    graph._run("MATCH (q:UsageQuota|UserQuota) DELETE q")

    def req(sub, email=None):
        user = {"sub": sub} | ({"email": email} if email else {})
        return SimpleNamespace(state=SimpleNamespace(user=user), client=None)

    alice = req("uuid-alice", "Alice@Example.com")
    try:
        assert quota.limit_for(alice) == 1000                 # no override: default
        quota.set_override("alice@example.com", 5000)          # set by email, any case
        assert quota.limit_for(alice) == 5000
        quota.set_override("uuid-alice", 7000)                 # subject beats email
        assert quota.limit_for(alice) == 7000
        assert quota.limit_for(req("uuid-bob")) == 1000        # others unaffected

        quota.charge(alice, {"input_tokens": 6000, "output_tokens": 0})
        quota.guard(alice)                                     # 6000 < 7000: allowed
        quota.set_override("uuid-alice", 0)                    # 0 = unlimited
        quota.charge(alice, {"input_tokens": 50_000, "output_tokens": 0})
        quota.guard(alice)

        assert quota.clear_override("uuid-alice") == 1
        assert quota.limit_for(alice) == 5000                  # email override remains
        rows = {r["user"]: r for r in quota.overview()}
        assert rows["uuid-alice"]["used_today"] == 56_000
        assert rows["alice@example.com"]["cap"] == 5000
    finally:
        graph._run("MATCH (q:UsageQuota|UserQuota) DELETE q")
        graph.close_driver()


# --- per-user usage and cost ---------------------------------------------------


def test_agent_totals_cost_per_model(monkeypatch):
    turns = iter([
        llm.LLMResponse([{"type": "tool_use", "id": "t1", "name": "find_symbol",
                          "input": {"query": "x"}}], "tool_use", 1000, 100,
                        model="claude-sonnet-4-6", cost_usd=0.0045),
        llm.LLMResponse([{"type": "text", "text": "done"}], "end_turn", 2000, 300,
                        model="claude-sonnet-4-6", cost_usd=0.0105),
    ])
    monkeypatch.setattr(llm, "create_message", lambda **k: next(turns))
    monkeypatch.setattr(tools, "run_tool", lambda n, i: [])
    u = agent.ask("q")["usage"]
    assert (u["input_tokens"], u["output_tokens"]) == (3000, 400)
    assert u["cost_usd"] == pytest.approx(0.015)
    assert u["by_model"] == {"claude-sonnet-4-6": {"input_tokens": 3000, "output_tokens": 400}}


def test_cost_is_priced_from_the_model_that_actually_ran():
    import telemetry

    assert telemetry.cost_of("claude-sonnet-4-6", 1_000_000, 0) > 0


@pytest.mark.skipif(not (URI and PASSWORD), reason="no Neo4j at NEO4J_TEST_URI")
def test_usage_report_and_me_endpoint_show_only_your_own(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("NEO4J_URI", URI)
    monkeypatch.setenv("NEO4J_PASSWORD", PASSWORD)
    graph.close_driver()
    graph.setup_schema()
    graph._run("MATCH (q:UsageQuota|UserQuota) DELETE q")

    def req(sub, email=None):
        user = {"sub": sub} | ({"email": email} if email else {})
        return SimpleNamespace(state=SimpleNamespace(user=user), client=None)

    try:
        quota.charge(req("local"), {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.006})
        quota.charge(req("local"), {"input_tokens": 3000, "output_tokens": 800, "cost_usd": 0.021})
        quota.charge(req("uuid-bob", "Bob@Example.com"),
                     {"input_tokens": 50_000, "output_tokens": 0, "cost_usd": 0.15})

        rows = quota.usage_report(30)
        by = {t["user"]: t for t in quota.totals(rows)}
        assert by["local"]["questions"] == 2 and by["local"]["tokens"] == 5000
        assert by["local"]["cost_usd"] == pytest.approx(0.027)
        assert by["uuid-bob"]["email"] == "bob@example.com"
        assert quota.totals(rows)[0]["user"] == "uuid-bob"  # most expensive first
        assert len(quota.usage_report(30, "BOB@example.com")) == 1  # by email, any case

        monkeypatch.setattr(quota, "limit_for", lambda request: 0)
        me = _client(monkeypatch).get("/me/usage").json()  # AUTH_MODE=none -> "local"
        assert me["user"] == "local"
        assert me["total"] == {"questions": 2, "tokens": 5000, "cost_usd": 0.027}
        assert all(d["user"] == "local" for d in me["days"])  # never Bob's rows
    finally:
        graph._run("MATCH (q:UsageQuota|UserQuota) DELETE q")
        graph.close_driver()
