"""Remembered conversations: stored in this stack, replayed append-only, capped,
and only ever visible to their owner."""

import os

import pytest
from fastapi.testclient import TestClient

import agent
import auth
import graph
import llm
import sessions

URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


# --- agent: where history goes (no database) ---------------------------------


def _capture(monkeypatch):
    sent = []

    def fake(*, system, messages, tools, model, max_tokens=2048):
        sent.append([dict(m) for m in messages])
        return llm.LLMResponse(content=[{"type": "text", "text": "answer"}], stop_reason="end_turn")

    monkeypatch.setattr(llm, "create_message", fake)
    return sent


def test_history_comes_before_the_question_unchanged(monkeypatch):
    sent = _capture(monkeypatch)
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    agent.ask("q2", max_steps=2, history=history)
    assert sent[0] == [*history, {"role": "user", "content": "q2"}]
    assert history == [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]


def test_context_stays_at_the_very_start(monkeypatch):
    """An impact report goes first, so it sits in the same place every turn and
    stays in the cached prefix."""
    sent = _capture(monkeypatch)
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    agent.ask("q2", max_steps=2, context="REPORT", history=history)
    assert sent[0][0]["content"] == "REPORT\n\nq1"
    assert sent[0][-1] == {"role": "user", "content": "q2"}


def test_without_history_nothing_changes(monkeypatch):
    sent = _capture(monkeypatch)
    agent.ask("q", max_steps=2, context="C")
    assert sent[0] == [{"role": "user", "content": "C\n\nq"}]


# --- with a database ------------------------------------------------------------


def _reachable() -> bool:
    if not URI or not PASSWORD:
        return False
    try:
        from neo4j import GraphDatabase

        d = GraphDatabase.driver(URI, auth=("neo4j", PASSWORD))
        d.verify_connectivity()
        d.close()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_neo4j = pytest.mark.skipif(not _reachable(), reason="no Neo4j at NEO4J_TEST_URI")


@pytest.fixture
def db():
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()
    graph.setup_schema()
    graph.clear_graph()
    yield
    graph.close_driver()


@pytest.fixture
def api(db, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_RATE_PER_MIN", "0")
    monkeypatch.setenv("DAILY_TOKEN_QUOTA", "0")
    import main

    who = {"user": "alice"}
    monkeypatch.setattr(auth, "user_key", lambda request: who["user"])
    monkeypatch.setattr(main.tools, "connect", lambda ids: {"nodes": [], "edges": []})
    seen = []

    def fake_ask(question, max_steps=None, *, history=None, **_):
        seen.append({"question": question, "history": [dict(m) for m in history or []]})
        return {"answer": f"answer to {question}", "cited_ids": ["core.f"], "usage": {}}

    monkeypatch.setattr(agent, "ask", fake_ask)
    return TestClient(main.app), seen, who


def _ask(c, q, **kw):
    return c.post("/ask", json={"question": q, "repo": "shop"} | kw)


@needs_neo4j
def test_without_remember_nothing_is_stored(api):
    c, seen, _ = api
    r = _ask(c, "q1")
    assert "session_id" not in r.json() and seen[0]["history"] == []
    assert graph._run("MATCH (s:Session) RETURN count(s) AS n") == [{"n": 0}]


@needs_neo4j
def test_a_remembered_conversation_replays_earlier_turns(api):
    c, seen, _ = api
    sid = _ask(c, "how does login work?", remember=True).json()["session_id"]
    r = _ask(c, "and who calls it?", remember=True, session_id=sid)
    assert r.json()["session_id"] == sid and r.json()["history_turns"] == 1
    history = seen[1]["history"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"].endswith("how does login work?")
    assert history[0]["content"].startswith(sessions.HISTORY_NOTE)  # replayed as untrusted
    assert history[1]["content"] == "answer to how does login work?"


@needs_neo4j
def test_history_is_append_only_so_the_cache_can_serve_it(api):
    c, seen, _ = api
    sid = _ask(c, "q1", remember=True).json()["session_id"]
    for q in ("q2", "q3", "q4"):
        _ask(c, q, remember=True, session_id=sid)
    for earlier, later in zip(seen[1:], seen[2:], strict=False):
        n = len(earlier["history"])
        assert later["history"][:n] == earlier["history"]  # byte-identical prefix


@needs_neo4j
def test_history_is_capped_by_turns_and_size_dropping_the_oldest(api, monkeypatch):
    c, seen, _ = api
    sid = _ask(c, "q1", remember=True).json()["session_id"]
    for q in ("q2", "q3", "q4"):
        _ask(c, q, remember=True, session_id=sid)
    monkeypatch.setenv("SESSION_MAX_TURNS", "2")
    _ask(c, "q5", remember=True, session_id=sid)
    texts = [m["content"] for m in seen[-1]["history"]]
    assert texts[0].endswith("q3") and texts[-1] == "answer to q4" and len(texts) == 4
    monkeypatch.setenv("SESSION_MAX_TURNS", "50")
    monkeypatch.setenv("SESSION_MAX_HISTORY_CHARS", "20")  # one short pair fits, two do not
    _ask(c, "q6", remember=True, session_id=sid)
    roles = [m["role"] for m in seen[-1]["history"]]
    assert roles == ["user", "assistant"]  # whole pairs only


@needs_neo4j
def test_another_users_conversation_is_not_found(api):
    c, seen, who = api
    sid = _ask(c, "secret question", remember=True).json()["session_id"]
    who["user"] = "mallory"
    assert _ask(c, "q", remember=True, session_id=sid).status_code == 404
    assert c.get(f"/sessions/{sid}").status_code == 404
    assert c.delete(f"/sessions/{sid}").status_code == 404
    assert c.get("/sessions", params={"repo": "shop"}).json() == []
    assert len(seen) == 1  # mallory's request never reached the model


@needs_neo4j
def test_a_conversation_stays_with_its_repo(api):
    c, _, _ = api
    sid = _ask(c, "q1", remember=True).json()["session_id"]
    r = c.post("/ask", json={"question": "q", "repo": "other", "remember": True, "session_id": sid})
    assert r.status_code == 400


@needs_neo4j
def test_list_open_and_delete(api):
    c, _, _ = api
    sid = _ask(c, "how does   login\nwork?", remember=True, mode="security").json()["session_id"]
    _ask(c, "next", remember=True, session_id=sid)
    listed = c.get("/sessions", params={"repo": "shop"}).json()
    assert [(s["id"], s["title"], s["turns"]) for s in listed] == [(sid, "how does login work?", 2)]
    opened = c.get(f"/sessions/{sid}").json()
    assert [t["question"] for t in opened["turns"]] == ["how does   login\nwork?", "next"]
    assert opened["turns"][0]["mode"] == "security" and opened["turns"][0]["cited"] == ["core.f"]
    assert c.delete(f"/sessions/{sid}").json() == {"status": "deleted"}
    assert c.get(f"/sessions/{sid}").status_code == 404
    assert graph._run("MATCH (t:Turn) RETURN count(t) AS n") == [{"n": 0}]


@needs_neo4j
def test_old_conversations_are_deleted(db, monkeypatch):
    old = sessions.create("alice", "shop")
    sessions.append(old, "q", "a", "explore")
    graph._run("MATCH (s:Session {id: $id}) SET s.updated_at = 1.0", id=old)
    sessions.create("alice", "shop")  # creating one prunes expired ones
    assert sessions.get(old, "alice") is None
    assert graph._run("MATCH (t:Turn) RETURN count(t) AS n") == [{"n": 0}]


@needs_neo4j
def test_session_ids_are_not_guessable(db):
    ids = {sessions.create("alice", "shop") for _ in range(20)}
    assert len(ids) == 20 and all(len(i) >= 20 for i in ids)


@needs_neo4j
def test_an_empty_conversation_is_not_listed(db):
    sessions.create("alice", "shop")
    assert sessions.list_for("alice", "shop") == []
