"""Ingestion resumability — the property that matters at 2M LOC.

A full index there is a multi-hour job. Without resumability a single rate limit
or dropped connection at hour 15 costs the entire run, and (because the old code
wiped first) left an empty database behind. These tests pin the guarantees:

  * every stage is idempotent and independently runnable
  * summarisation reads its work queue from the graph, so it resumes
  * a failed summary stays pending instead of being poisoned with a fallback
  * nothing is destroyed unless `fresh=True` is passed explicitly
"""

import os

import pytest

import graph
import indexer
import summarize

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "webforms_app")
URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


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


pytestmark = pytest.mark.skipif(not _reachable(), reason="no Neo4j at NEO4J_TEST_URI")


@pytest.fixture(autouse=True)
def clean_graph():
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()
    graph.setup_schema()
    graph.clear_graph()
    yield
    graph.clear_graph()


def test_parse_stage_runs_alone_and_leaves_everything_pending():
    stats = indexer.index_repo(FIXTURE, stages=("parse",))
    assert stats["symbols"] == 19
    assert len(indexer.pending_summaries()) == 19


def test_summarize_resumes_after_failures(monkeypatch):
    """The core guarantee: a crashed run continues instead of restarting."""
    indexer.index_repo(FIXTURE, stages=("parse",))

    fail_ids = {s.id for s in indexer.pending_summaries()[:12]}
    attempts = []

    def flaky(sym):
        attempts.append(sym.id)
        if sym.id in fail_ids:
            raise RuntimeError("simulated 429")
        return f"summary of {sym.name}"

    monkeypatch.setattr(summarize, "summarize_symbol", flaky)
    first = indexer.write_summaries(workers=4, progress_every=10_000)
    assert first["summarized"] == 19 - len(fail_ids)
    assert first["failed"] == len(fail_ids)
    # only the failures are still queued
    assert {s.id for s in indexer.pending_summaries()} == fail_ids

    attempts.clear()

    def reliable(sym):
        attempts.append(sym.id)
        return f"summary of {sym.name}"

    monkeypatch.setattr(summarize, "summarize_symbol", reliable)
    second = indexer.write_summaries(workers=4, progress_every=10_000)

    assert second["summarized"] == len(fail_ids)
    assert set(attempts) == fail_ids, "second pass must not redo completed work"
    assert indexer.pending_summaries() == []


def test_failed_summary_is_not_poisoned_with_the_symbol_name(monkeypatch):
    """The old fallback wrote `summary = name`, which made the symbol look done
    forever and quietly degraded the search index."""
    indexer.index_repo(FIXTURE, stages=("parse",))
    monkeypatch.setattr(summarize, "llm", type("X", (), {
        "complete": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
        "SUMMARY_MODEL": "m",
    }))
    indexer.write_summaries(workers=2, progress_every=10_000)
    assert len(indexer.pending_summaries()) == 19, "failures must remain retryable"


def test_summaries_are_idempotent(monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"summary of {s.name}")
    indexer.index_repo(FIXTURE, stages=("parse",))
    indexer.write_summaries(workers=4, progress_every=10_000)
    again = indexer.write_summaries(workers=4, progress_every=10_000)
    assert again == {"summarized": 0, "failed": 0, "pending": 0}


def test_community_stage_runs_without_reparsing(monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"summary of {s.name}")
    monkeypatch.setattr(summarize, "summarize_community", lambda n, m: f"cluster {n}")
    indexer.index_repo(FIXTURE, stages=("parse", "summarize"))

    stats = indexer.index_repo(FIXTURE, stages=("community",))
    assert stats["communities"] >= 1
    assert graph._run("MATCH (m:RepoMeta) RETURN count(m) AS n")[0]["n"] == 1


def test_indexing_is_not_destructive_by_default(monkeypatch):
    """`fresh` must be opt-in — a crash at hour 15 must not leave an empty DB."""
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"summary of {s.name}")
    indexer.index_repo(FIXTURE, stages=("parse",))
    graph._run("MERGE (s:Symbol {id:'sentinel'}) SET s.summary='keep me'")

    indexer.index_repo(FIXTURE, stages=("parse",))          # default: no wipe
    assert graph._run("MATCH (s:Symbol {id:'sentinel'}) RETURN count(s) AS n")[0]["n"] == 1

    indexer.index_repo(FIXTURE, stages=("parse",), fresh=True)
    assert graph._run("MATCH (s:Symbol {id:'sentinel'}) RETURN count(s) AS n")[0]["n"] == 0


# --- re-indexing after a teammate pushes ------------------------------------


@pytest.fixture
def workdir(tmp_path):
    import shutil

    dest = tmp_path / "app"
    shutil.copytree(FIXTURE, dest)
    return str(dest)


def _counting_summarizer(counter):
    def fake(sym):
        counter.append(sym.id)
        return f"summary of {sym.name}"

    return fake


def test_reindex_with_no_changes_costs_nothing(monkeypatch, workdir):
    """The bug this pins: the parse-time dataclass carries summary="", and
    `SET s += row` used to clobber real summaries with it — so every re-index
    re-summarised the entire repo at full price."""
    seen: list[str] = []
    monkeypatch.setattr(summarize, "summarize_symbol", _counting_summarizer(seen))

    indexer.index_repo(workdir, repo="t", stages=("parse", "summarize"))
    first = len(seen)
    assert first > 0

    seen.clear()
    indexer.index_repo(workdir, repo="t", stages=("parse", "summarize"))
    assert seen == [], "unchanged code must not be re-summarised"


def test_reindex_resummarizes_only_what_changed(monkeypatch, workdir):
    seen: list[str] = []
    monkeypatch.setattr(summarize, "summarize_symbol", _counting_summarizer(seen))
    indexer.index_repo(workdir, repo="t", stages=("parse", "summarize"))

    target = os.path.join(workdir, "Web", "Orders", "OrderList.aspx.cs")
    with open(target) as fh:
        body = fh.read()
    with open(target, "w") as fh:
        fh.write(body.replace('Audit("save");', 'Audit("save"); LogMetric();'))

    seen.clear()
    indexer.index_repo(workdir, repo="t", stages=("parse", "summarize"))
    assert seen, "edited code must be re-summarised"
    assert all("OrderList" in sid for sid in seen), seen
    assert len(seen) < 6, "only the touched symbols, not the whole repo"


def test_deleted_code_is_removed_from_the_graph(monkeypatch, workdir):
    """MERGE only adds and updates. Without stale deletion a removed method
    lingers forever and blast radius reports dependencies on code that is gone."""
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: "x")
    indexer.index_repo(workdir, repo="t", stages=("parse",))
    assert graph._run(
        "MATCH (s:Symbol) WHERE s.id STARTS WITH 'Acme.Services.OrderService' "
        "RETURN count(s) AS n"
    )[0]["n"] > 0

    os.remove(os.path.join(workdir, "Services", "OrderService.cs"))
    stats = indexer.index_repo(workdir, repo="t", stages=("parse",))

    assert stats["removed"] > 0
    assert graph._run(
        "MATCH (s:Symbol) WHERE s.id STARTS WITH 'Acme.Services.OrderService' "
        "RETURN count(s) AS n"
    )[0]["n"] == 0


def test_deletion_is_scoped_to_one_repo(monkeypatch, workdir):
    """Indexing one codebase must never delete another sharing the database."""
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: "x")
    graph._run("MERGE (s:Symbol {id:'other.thing'}) SET s.repo='otherapp', s.summary='keep'")

    indexer.index_repo(workdir, repo="t", stages=("parse",))
    assert graph._run(
        "MATCH (s:Symbol {id:'other.thing'}) RETURN count(s) AS n"
    )[0]["n"] == 1
