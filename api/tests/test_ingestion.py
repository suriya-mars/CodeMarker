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
import llm
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
    graph.wipe_test_database()
    yield
    graph.wipe_test_database()


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
        "summary_model": staticmethod(lambda: "m"),
    }))
    indexer.write_summaries(workers=2, progress_every=10_000)
    assert len(indexer.pending_summaries()) == 19, "failures must remain retryable"


def test_summaries_are_idempotent(monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"summary of {s.name}")
    indexer.index_repo(FIXTURE, stages=("parse",))
    indexer.write_summaries(workers=4, progress_every=10_000)
    again = indexer.write_summaries(workers=4, progress_every=10_000)
    assert again == {"summarized": 0, "reused": 0, "failed": 0, "pending": 0}


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

    # fresh clears THIS repo's code graph only: its own leftovers go, another
    # repo's symbols stay
    repo = os.path.basename(FIXTURE)
    graph._run("MATCH (s:Symbol {id:'sentinel'}) SET s.repo = $repo", repo=repo)
    graph._run("CREATE (:Symbol {id: 'other.repo.sym', repo: 'other'})")
    indexer.index_repo(FIXTURE, stages=("parse",), fresh=True)
    assert graph._run("MATCH (s:Symbol {id:'sentinel'}) RETURN count(s) AS n")[0]["n"] == 0
    assert graph._run("MATCH (s:Symbol {repo:'other'}) RETURN count(s) AS n")[0]["n"] == 1


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


def test_community_rerun_leaves_no_stale_communities(monkeypatch):
    """Leiden renumbers on every run, and deleted code must not stay listed."""
    monkeypatch.setattr(summarize, "summarize_community", lambda n, m: f"cluster {n}")
    graph._run("CREATE (:Symbol {id: 'a'}), (:Symbol {id: 'b'})")
    indexer.write_communities({"a": "old", "b": "old"}, {"old": ["a", "b"]}, {})
    indexer.write_communities({"a": "new"}, {"new": ["a"]}, {})
    assert graph._run("MATCH (c:Community) RETURN c.id AS id") == [{"id": "new"}]
    assert graph._run(
        "MATCH (s:Symbol)-[:IN_COMMUNITY]->(c) RETURN s.id AS s, c.id AS c"
    ) == [{"s": "a", "c": "new"}]
    rows = graph._run("MATCH (s:Symbol {id: 'b'}) RETURN s.community_id AS c")
    assert rows == [{"c": None}]


# --- community summary cache ----------------------------------------------------


def _community_world():
    graph._run("CREATE (:Symbol {id: 'a'}), (:Symbol {id: 'b'}), (:Symbol {id: 'c'})")
    return {"a": "reads the file", "b": "parses it", "c": "writes it"}


def _paid(monkeypatch):
    calls = []

    def fake(name, summaries):
        calls.append(summaries)
        return f"cluster of {len(summaries)}"

    monkeypatch.setattr(summarize, "summarize_community", fake)
    return calls


def test_unchanged_community_is_not_paid_for_twice(monkeypatch):
    calls = _paid(monkeypatch)
    s = _community_world()
    first = indexer.write_communities({"a": "c0", "b": "c0"}, {"c0": ["a", "b"]}, s)
    again = indexer.write_communities({"a": "c0", "b": "c0"}, {"c0": ["a", "b"]}, s)
    assert len(calls) == 1
    assert first["community_summaries_generated"] == 1
    assert again == {"community_summaries_generated": 0, "community_summaries_reused": 1,
                     "community_summaries_failed": 0}
    assert graph._run("MATCH (c:Community) RETURN c.summary AS s") == [{"s": "cluster of 2"}]


def test_leiden_renumbering_still_hits_the_cache(monkeypatch):
    calls = _paid(monkeypatch)
    s = _community_world()
    indexer.write_communities({"a": "c0", "b": "c0"}, {"c0": ["a", "b"]}, s)
    indexer.write_communities({"a": "c9", "b": "c9"}, {"c9": ["b", "a"]}, s)
    assert len(calls) == 1
    assert graph._run("MATCH (c:Community) RETURN c.id AS i, c.summary AS s") == [
        {"i": "c9", "s": "cluster of 2"}]


@pytest.mark.parametrize(("members", "summaries"), [
    (["a", "b", "c"], {}),  # a member joined
    (["a"], {}),  # a member left
    (["a", "b"], {"b": "now does something else"}),  # a member's code changed
])
def test_a_real_change_regenerates(monkeypatch, members, summaries):
    calls = _paid(monkeypatch)
    s = _community_world()
    indexer.write_communities({"a": "c0", "b": "c0"}, {"c0": ["a", "b"]}, s)
    indexer.write_communities({m: "c0" for m in members}, {"c0": members}, s | summaries)
    assert len(calls) == 2


def test_only_the_changed_community_regenerates(monkeypatch):
    calls = _paid(monkeypatch)
    s = _community_world()
    both = {"c0": ["a", "b"], "c1": ["c"]}
    indexer.write_communities({"a": "c0", "b": "c0", "c": "c1"}, both, s)
    stats = indexer.write_communities({"a": "c0", "b": "c0", "c": "c1"}, both, s | {"c": "new"})
    assert len(calls) == 3
    assert stats["community_summaries_generated"] == 1
    assert stats["community_summaries_reused"] == 1


def test_a_failed_summary_is_retried_not_cached(monkeypatch):
    s = _community_world()
    monkeypatch.setattr(summarize, "summarize_community", lambda name, m: name)  # the fallback
    stats = indexer.write_communities({"a": "c0"}, {"c0": ["a"]}, s)
    assert stats["community_summaries_failed"] == 1
    assert graph._run("MATCH (c:CommunitySummary) RETURN count(c) AS n") == [{"n": 0}]

    calls = _paid(monkeypatch)
    stats = indexer.write_communities({"a": "c0"}, {"c0": ["a"]}, s)
    assert len(calls) == 1
    assert stats["community_summaries_generated"] == 1


def test_a_crash_keeps_what_was_paid_for_and_the_old_communities(monkeypatch):
    s = _community_world()
    calls = _paid(monkeypatch)
    indexer.write_communities({"a": "old"}, {"old": ["a"]}, s)

    def dies_on_second(name, summaries):
        calls.append(summaries)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return "paid for"

    monkeypatch.setattr(summarize, "summarize_community", dies_on_second)
    new = {"c0": ["b"], "c1": ["c"]}
    with pytest.raises(KeyboardInterrupt):
        indexer.write_communities({"b": "c0", "c": "c1"}, new, s)
    # the graph still shows the previous communities
    assert graph._run("MATCH (c:Community) RETURN c.id AS id") == [{"id": "old"}]

    calls.clear()
    monkeypatch.setattr(summarize, "summarize_community",
                        lambda n, m: calls.append(m) or "resumed")
    stats = indexer.write_communities({"b": "c0", "c": "c1"}, new, s)
    assert len(calls) == 1  # only the one never paid for
    assert stats["community_summaries_reused"] == 1


def test_a_member_past_the_prompt_cap_does_not_force_a_new_summary(monkeypatch):
    calls = _paid(monkeypatch)
    ids = [f"m{i:03d}" for i in range(summarize._MAX_COMMUNITY_MEMBERS + 5)]
    graph._run("UNWIND $ids AS i CREATE (:Symbol {id: i})", ids=ids)
    text = {i: f"does {i}" for i in ids}
    indexer.write_communities({i: "c0" for i in ids}, {"c0": ids}, text)
    indexer.write_communities({i: "c0" for i in ids}, {"c0": ids}, text | {ids[-1]: "changed"})
    assert len(calls) == 1
    # the count is still right, because it does not come from the summary
    grown = [*ids, "m999"]
    graph._run("CREATE (:Symbol {id: 'm999'})")
    indexer.write_communities({i: "c0" for i in grown}, {"c0": grown}, text)
    assert graph._run("MATCH (c:Community) RETURN c.member_count AS n")[0]["n"] == len(grown)


def test_unused_cache_entries_expire(monkeypatch):
    _paid(monkeypatch)
    s = _community_world()
    graph._run("CREATE (:CommunitySummary {fingerprint: 'old', summary: 'x', last_used: 1.0})")
    graph._run("CREATE (:CommunitySummary {fingerprint: 'recent', summary: 'y', last_used: $t})",
               t=__import__("time").time() - 86400)
    indexer.write_communities({"a": "c0"}, {"c0": ["a"]}, s)
    left = {r["f"] for r in graph._run("MATCH (c:CommunitySummary) RETURN c.fingerprint AS f")}
    assert "old" not in left and "recent" in left


# --- summary cache: identical input is never paid for twice ---------------------


def _counting(monkeypatch):
    calls = []
    monkeypatch.setattr(summarize, "summarize_symbol",
                        lambda s: calls.append(s.id) or f"does {s.name}")
    return calls


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


BODY = "def load(path):\n    return open(path).read()\n"


def test_the_same_code_in_another_repo_is_not_summarized_again(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    _write(tmp_path, "one/io_util.py", BODY)
    _write(tmp_path, "two/pkg/io_util.py", BODY)  # same file, different id
    indexer.index_repo(str(tmp_path / "one"), repo="one", stages=("parse", "summarize"))
    first = len(calls)
    stats = indexer.index_repo(str(tmp_path / "two"), repo="two", stages=("parse", "summarize"))
    assert first == 2 and len(calls) == first  # file + function, then nothing new
    assert stats["reused"] == 2 and stats["summarized"] == 0
    rows = graph._run("MATCH (s:Symbol {repo: 'two'}) RETURN s.summary AS s ORDER BY s")
    assert [r["s"] for r in rows] == ["does io_util.py", "does load"]  # written by repo one's run


def test_a_moved_file_reuses_its_summaries(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    _write(tmp_path, "r/old/io_util.py", BODY)
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    calls.clear()
    (tmp_path / "r" / "old" / "io_util.py").rename(tmp_path / "r" / "io_util.py")
    stats = indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert calls == [] and stats["reused"] == 2


def test_changed_code_is_summarized_again(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    _write(tmp_path, "r/io_util.py", BODY)
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    calls.clear()
    _write(tmp_path, "r/io_util.py", BODY.replace(".read()", ".read().strip()"))
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert "io_util.load" in calls


def test_identical_symbols_in_one_run_share_one_call(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    for d in ("a", "b", "c"):
        _write(tmp_path, f"r/{d}/io_util.py", BODY)
    stats = indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert len(calls) == 2  # one file summary, one function summary, for all three copies
    assert stats["reused"] == 4
    assert graph._run("MATCH (s:Symbol {repo: 'r'}) WHERE s.summary IS NULL RETURN count(s) AS n"
                      ) == [{"n": 0}]


def test_a_different_model_regenerates(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    _write(tmp_path, "one/io_util.py", BODY)
    _write(tmp_path, "two/pkg/io_util.py", BODY)  # same file, different id
    indexer.index_repo(str(tmp_path / "one"), repo="one", stages=("parse", "summarize"))
    monkeypatch.setattr(llm, "summary_model", lambda: "a-better-model")
    calls.clear()
    indexer.index_repo(str(tmp_path / "two"), repo="two", stages=("parse", "summarize"))
    assert len(calls) == 2


def test_a_new_prompt_version_regenerates(tmp_path, monkeypatch):
    calls = _counting(monkeypatch)
    _write(tmp_path, "one/io_util.py", BODY)
    _write(tmp_path, "two/pkg/io_util.py", BODY)  # same file, different id
    indexer.index_repo(str(tmp_path / "one"), repo="one", stages=("parse", "summarize"))
    monkeypatch.setattr(summarize, "PROMPT_VERSION", "2")
    calls.clear()
    indexer.index_repo(str(tmp_path / "two"), repo="two", stages=("parse", "summarize"))
    assert len(calls) == 2


def test_a_failed_summary_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: None)
    _write(tmp_path, "r/io_util.py", BODY)
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert graph._run("MATCH (c:SummaryCache) RETURN count(c) AS n") == [{"n": 0}]
    calls = _counting(monkeypatch)
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert len(calls) == 2


def test_a_flagged_summary_stays_flagged_when_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(summarize, "summarize_symbol", lambda s: f"{summarize.FLAG} reads a file")
    _write(tmp_path, "one/io_util.py", BODY)
    _write(tmp_path, "two/pkg/io_util.py", BODY)  # same file, different id
    indexer.index_repo(str(tmp_path / "one"), repo="one", stages=("parse", "summarize"))
    indexer.index_repo(str(tmp_path / "two"), repo="two", stages=("parse", "summarize"))
    rows = graph._run("MATCH (s:Symbol {repo: 'two'}) RETURN s.summary AS s")
    assert all(r["s"].startswith("[flagged") for r in rows)


def test_the_key_ignores_location_but_not_what_the_prompt_shows():
    from parsing import Symbol

    def sym(**kw):
        base = dict(id="a.f", kind="function", name="f", path="a.py", start_line=1, end_line=2,
                    language="python", signature="f(x)", source=BODY)
        return Symbol(**(base | kw))

    key = summarize.summary_key
    assert key(sym()) == key(sym(id="b.f", path="b/a.py", start_line=40, end_line=41))
    assert key(sym()) != key(sym(signature="f(x, y)"))
    assert key(sym()) != key(sym(source=BODY + "# changed\n"))
    assert key(sym()) != key(sym(kind="method"))
    # beyond what the prompt sends, a difference does not matter
    long = "x = 1\n" * 1000
    assert key(sym(source=long + "a")) == key(sym(source=long + "b"))


def test_unused_summary_cache_entries_expire(tmp_path, monkeypatch):
    _counting(monkeypatch)
    graph._run("CREATE (:SummaryCache {key: 'old', summary: 'x', last_used: 1.0})")
    _write(tmp_path, "r/io_util.py", BODY)
    indexer.index_repo(str(tmp_path / "r"), repo="r", stages=("parse", "summarize"))
    assert graph._run("MATCH (c:SummaryCache {key: 'old'}) RETURN count(c) AS n") == [{"n": 0}]
