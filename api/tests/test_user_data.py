"""User data survives re-indexing, expires on time, and can be forgotten."""

import datetime as dt
import os

import pytest

import graph
import indexer
import quota
import sessions
import summarize

URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


def test_the_full_wipe_refuses_outside_the_test_database(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://production:7687")
    monkeypatch.setenv("NEO4J_TEST_URI", "bolt://localhost:7688")
    with pytest.raises(RuntimeError, match="refusing to wipe"):
        graph.wipe_test_database()
    monkeypatch.delenv("NEO4J_TEST_URI")
    with pytest.raises(RuntimeError, match="refusing to wipe"):
        graph.wipe_test_database()


def test_clearing_needs_a_repo_name():
    with pytest.raises(ValueError):
        graph.clear_code_graph("")


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
def db(monkeypatch):
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()
    graph.setup_schema()
    graph.wipe_test_database()
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"does {s.name}")
    monkeypatch.setattr(summarize, "summarize_community", lambda n, m: "a cluster")
    yield
    graph.close_driver()


def _day(offset: int) -> str:
    return (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=offset)).isoformat()


def _usage(user, email, day):
    graph._run("CREATE (:UsageQuota {key: $k, user: $u, email: $e, day: $d, tokens: 10})",
               k=f"{day}|{user}", u=user, e=email, d=day)


def _count(label):
    return graph._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# --- 1. a full re-index touches only its own repo's code graph ---------------------


@needs_neo4j
def test_a_fresh_reindex_keeps_user_data_and_other_repos(db, tmp_path):
    _write(tmp_path, "one/a.py", "def f():\n    return 1\n")
    _write(tmp_path, "two/pkg/b.py", "def g():\n    return 2\n")
    indexer.index_repo(str(tmp_path / "one"), repo="one")
    indexer.index_repo(str(tmp_path / "two"), repo="two")
    _usage("sub-alice", "alice@acme.com", _day(0))
    quota.set_override("alice@acme.com", 5000)
    sid = sessions.create("sub-alice", "one")
    sessions.append(sid, "q", "a", "explore")
    graph._run("CREATE (:Repo {name: 'one', added_by: 'sub-alice', status: 'ready'})")
    graph._run("CREATE (:ImpactReport {id: 'r1', repo: 'one'})")
    caches = _count("SummaryCache")

    stats = indexer.index_repo(str(tmp_path / "one"), repo="one", fresh=True)

    assert stats["symbols"] == 2  # re-indexed
    assert _count("UsageQuota") == 1 and _count("UserQuota") == 1
    assert _count("Session") == 1 and _count("Turn") == 1
    assert _count("Repo") == 1 and _count("ImpactReport") == 1
    assert _count("SummaryCache") == caches  # nothing paid for twice
    two = graph._run("MATCH (s:Symbol {repo: 'two'}) RETURN count(s) AS n")[0]["n"]
    assert two == 2  # the other repo is untouched


@needs_neo4j
def test_clearing_a_repo_removes_its_symbols_edges_and_commit(db, tmp_path):
    _write(tmp_path, "one/a.py", "def f():\n    return g()\n\n\ndef g():\n    return 1\n")
    indexer.index_repo(str(tmp_path / "one"), repo="one", stages=("parse",))
    graph._run("MERGE (:RepoState {repo: 'one', commit: 'abc'})")
    assert graph.clear_code_graph("one") == 3
    assert graph._run("MATCH (s:Symbol {repo: 'one'}) RETURN count(s) AS n")[0]["n"] == 0
    assert graph._run("MATCH ()-[r:CALLS]->() RETURN count(r) AS n")[0]["n"] == 0
    assert _count("RepoState") == 0


# --- 2. retention ----------------------------------------------------------------------


@needs_neo4j
def test_usage_older_than_retention_is_deleted(db, monkeypatch):
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "30")
    for days_ago in (0, 29, 30, 31, 400):
        _usage("u", None, _day(days_ago))
    assert quota.prune() == 2  # 31 and 400 days ago
    left = sorted(r["d"] for r in graph._run("MATCH (q:UsageQuota) RETURN q.day AS d"))
    assert left == sorted([_day(0), _day(29), _day(30)])


@needs_neo4j
def test_zero_retention_keeps_everything(db, monkeypatch):
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "0")
    _usage("u", None, _day(5000))
    assert quota.prune() == 0 and _count("UsageQuota") == 1


@needs_neo4j
def test_retention_runs_by_itself_once_a_day(db, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("USAGE_RETENTION_DAYS", "30")
    monkeypatch.setattr(quota, "_pruned_on", None)
    _usage("old", None, _day(100))
    request = SimpleNamespace(state=SimpleNamespace(user={"sub": "alice"}), client=None)
    quota.charge(request, {"input_tokens": 5, "output_tokens": 1})
    assert graph._run("MATCH (q:UsageQuota {user: 'old'}) RETURN count(q) AS n")[0]["n"] == 0
    _usage("old2", None, _day(100))
    quota.charge(request, {"input_tokens": 5, "output_tokens": 1})  # same day: no second prune
    assert graph._run("MATCH (q:UsageQuota {user: 'old2'}) RETURN count(q) AS n")[0]["n"] == 1


# --- 3. forget one person -------------------------------------------------------------


def _alice_and_bob():
    _usage("sub-alice", "alice@acme.com", _day(0))
    _usage("sub-alice", "alice@acme.com", _day(1))
    _usage("sub-bob", "bob@acme.com", _day(0))
    quota.set_override("alice@acme.com", 5000)
    quota.set_override("sub-bob", 100)
    a = sessions.create("sub-alice", "shop")
    sessions.append(a, "alice's question", "answer", "explore")
    b = sessions.create("sub-bob", "shop")
    sessions.append(b, "bob's question", "answer", "explore")
    graph._run("CREATE (:Repo {name: 'shop', added_by: 'sub-alice', status: 'ready'})")
    graph._run("CREATE (:Repo {name: 'lib', added_by: 'sub-bob', status: 'ready'})")


@needs_neo4j
def test_forget_by_email_finds_the_login_id_too(db):
    _alice_and_bob()
    r = quota.forget("Alice@Acme.com", dry_run=False)
    assert set(r["identities"]) == {"alice@acme.com", "sub-alice"}
    assert (r["usage"], r["overrides"], r["conversations"], r["turns"], r["repos"]) == (2, 1, 1, 1, 1)
    assert graph._run("MATCH (q:UsageQuota) RETURN collect(q.user) AS u")[0]["u"] == ["sub-bob"]
    assert graph._run("MATCH (u:UserQuota) RETURN collect(u.user) AS u")[0]["u"] == ["sub-bob"]
    assert graph._run("MATCH (s:Session) RETURN collect(s.user) AS u")[0]["u"] == ["sub-bob"]
    assert graph._run("MATCH (t:Turn) RETURN collect(t.question) AS q")[0]["q"] == ["bob's question"]
    repos = {r["n"]: r["by"] for r in graph._run("MATCH (r:Repo) RETURN r.name AS n, r.added_by AS by")}
    assert repos == {"shop": "a removed user", "lib": "sub-bob"}  # the repo stays


@needs_neo4j
def test_forget_by_login_id(db):
    _alice_and_bob()
    quota.forget("sub-alice", dry_run=False)
    assert graph._run("MATCH (q:UsageQuota {email: 'alice@acme.com'}) RETURN count(q) AS n")[0]["n"] == 0


@needs_neo4j
def test_forget_only_shows_unless_told_to_delete(db):
    _alice_and_bob()
    r = quota.forget("alice@acme.com")
    assert r["deleted"] is False and r["usage"] == 2
    assert _count("UsageQuota") == 3 and _count("Session") == 2


@needs_neo4j
def test_forgetting_someone_unknown_changes_nothing(db):
    _alice_and_bob()
    r = quota.forget("nobody@acme.com", dry_run=False)
    assert (r["usage"], r["conversations"], r["repos"]) == (0, 0, 0)
    assert _count("UsageQuota") == 3
