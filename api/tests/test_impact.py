"""Commit impact analysis.

Unit tests pin the symbol-level diff and the agent wiring with no database.
The integration test builds a real two-commit git repo, indexes the FIRST
commit, then analyses the second — the exact production sequence — and asserts
the blast radius, paths, and risk flags against a live Neo4j.
"""

import os
import re
import subprocess

import pytest

import agent
import impact
import llm
import tools
from parsing import Symbol


def _sym(sid, kind="function", path="core.py", source="", signature=""):
    return Symbol(id=sid, kind=kind, name=sid.split(".")[-1], path=path,
                  start_line=1, end_line=1, language="python",
                  source=source, signature=signature)


def _row(s: Symbol, summary=""):
    return {"id": s.id, "kind": s.kind, "name": s.name, "path": s.path,
            "signature": s.signature, "source": s.source, "summary": summary}


# --- unit: symbol diff ------------------------------------------------------


def test_diff_classifies_and_drops_explained_containers():
    old = [
        _sym("core", "file", source="v1"),
        _sym("core.a", source="def a(): 1", signature="a()"),
        _sym("core.b", source="def b(): 1", signature="b()"),
        _sym("core.gone", source="def gone(): 1", signature="gone()"),
        _sym("core.same", source="def same(): 1", signature="same()"),
    ]
    new = [
        _sym("core", "file", source="v2"),
        _sym("core.a", source="def a(): 2", signature="a()"),
        _sym("core.b", source="def b(x): 1", signature="b(x)"),
        _sym("core.same", source="def same(): 1", signature="same()"),
        _sym("core.fresh", source="def fresh(): 1", signature="fresh()"),
    ]
    changed = impact.diff_symbols([_row(s, f"old {s.name}") for s in old], new)
    got = {c.id: c.change for c in changed}
    # the file changed only because its members did -> not reported
    assert got == {"core.gone": "removed", "core.b": "signature_changed",
                   "core.a": "modified", "core.fresh": "added"}
    b = next(c for c in changed if c.id == "core.b")
    assert (b.old_signature, b.new_signature) == ("b()", "b(x)")
    assert b.summary == "old b"  # the graph's summary of the code before the change
    assert "-def b(): 1" in b.diff and "+def b(x): 1" in b.diff


def test_container_change_with_no_member_change_is_reported():
    """A module-level edit (a constant, an import) touches no function, but it
    is still a change and must not vanish."""
    old = [_sym("core", "file", source="X = 1"), _sym("core.a", source="def a(): 1")]
    new = [_sym("core", "file", source="X = 2"), _sym("core.a", source="def a(): 1")]
    changed = impact.diff_symbols([_row(s) for s in old], new)
    assert [(c.id, c.change) for c in changed] == [("core", "modified")]


def test_merged_partial_class_is_not_falsely_modified():
    """A C# partial class's graph source is the MERGE of several files, so it
    can never equal one file's parse. Comparing them would flag every partial
    class on every commit."""
    old = [{**_row(_sym("Web.Page", "class", source="merged")),
            "path": "Page.aspx.cs;Page.aspx.designer.cs"}]
    new = [_sym("Web.Page", "class", path="Page.aspx.cs", source="one part")]
    assert impact.diff_symbols(old, new) == []


def test_risk_flags_only_untouched_dependents():
    changed = [
        impact.ChangedSymbol("core.gone", "function", "gone", "core.py", "removed"),
        impact.ChangedSymbol("core.b", "function", "b", "core.py", "signature_changed",
                             old_signature="b()", new_signature="b(x)"),
        impact.ChangedSymbol("api.h", "function", "h", "api.py", "modified"),
    ]
    deps = {"core.gone": ["api.old"], "core.b": ["api.h"]}  # api.h was updated too
    risk = impact.assess_risk(changed, [], deps, [])
    codes = {f["code"]: f for f in risk["flags"]}
    assert codes["REMOVED_BUT_STILL_REFERENCED"]["dependents"] == ["api.old"]
    assert "SIGNATURE_CHANGED_CALLERS_UNTOUCHED" not in codes
    assert risk["level"] == "medium"  # a hard flag never leaves risk at "low"


# --- unit: the LLM sees the digest, a narrowed toolset, and lights report ids


def test_impact_ask_uses_digest_and_restricted_tools(monkeypatch):
    report = impact.ImpactReport(
        id="r1", repo="demo", base="a" * 40, head="b" * 40, created_at=0.0,
        files=[{"status": "M", "path": "core.py", "old_path": "core.py"}],
        changed=[impact.ChangedSymbol("core.helper", "function", "helper", "core.py",
                                      "modified", summary="adds one", diff="-1\n+2")],
        affected=[impact.AffectedSymbol("api.handler", "function", "handler", "api.py",
                                        "serves requests", 2, "core.helper", "CALLS",
                                        ["api.handler", "core.compute", "core.helper"])],
        importers=[], communities=[],
        risk={"level": "low", "score": 1, "flags": [], "affected_pages": 0,
              "affected_tests": 0},
        warnings=[],
    )
    seen = {}

    def fake_create_message(*, system, messages, tools, model, max_tokens=2048):
        seen.update(system=system, first=messages[0]["content"],
                    tools=[t["name"] for t in tools])
        return llm.LLMResponse(
            content=[{"type": "text", "text": "`core.helper` reaches `api.handler`."}],
            stop_reason="end_turn")

    monkeypatch.setattr(llm, "create_message", fake_create_message)
    out = impact.ask(report, "what breaks?")

    # report in the USER turn, inside a random boundary the system prompt names
    tag = re.match(r"<(impact-report-[0-9a-f]{16})>", seen["first"]).group(1)
    assert seen["system"] == impact.impact_system(tag)
    assert f"</{tag}>" in seen["first"] and "<impact_report>" not in seen["system"]
    assert "serves requests" in seen["first"]  # summaries stand in for source
    assert seen["first"].rstrip().endswith("what breaks?")
    assert set(seen["tools"]) == set(impact.IMPACT_TOOLS)
    # ids cited from the report itself are lit, with no tool call needed
    assert out["cited_ids"] == ["api.handler", "core.helper"]
    assert out["report_id"] == "r1"


def test_tool_outside_subset_is_refused(monkeypatch):
    """A narrowed toolset is enforced, not just advertised."""
    turns = iter([
        llm.LLMResponse(content=[{"type": "tool_use", "id": "t1", "name": "find_symbol",
                                  "input": {"query": "x"}}], stop_reason="tool_use"),
        llm.LLMResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    captured = {}

    def fake_create_message(*, system, messages, tools, model, max_tokens=2048):
        captured["messages"] = list(messages)
        return next(turns)

    def boom(name, tool_input):
        raise AssertionError("must not run a tool outside the subset")

    monkeypatch.setattr(llm, "create_message", fake_create_message)
    monkeypatch.setattr(tools, "run_tool", boom)
    agent.ask("q", tool_names=("get_definition",))
    block = captured["messages"][-1]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "t1"
    assert block["is_error"] is True


# --- integration: real git, real index, real Neo4j ---------------------------

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


V1 = {
    "core.py": (
        "def helper(x):\n    return x + 1\n\n\n"
        "def compute(x):\n    return helper(x)\n\n\n"
        "def legacy():\n    return 0\n"
    ),
    "api.py": (
        "from core import compute, legacy\n\n\n"
        "def handler(req):\n    return compute(req)\n\n\n"
        "def old_handler():\n    return legacy()\n"
    ),
    "tests/test_core.py": (
        "from core import compute\n\n\n"
        "def test_compute():\n    assert compute(1) == 2\n"
    ),
    "README.txt": "demo\n",
}
# commit 2: helper's body changes, legacy is deleted (old_handler still calls
# it), a new function appears, and a non-code file changes
V2_CORE = (
    "def helper(x):\n    return x + 2\n\n\n"
    "def compute(x):\n    return helper(x)\n\n\n"
    "def fresh():\n    return 1\n"
)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.mark.skipif(not _reachable(), reason="no Neo4j at NEO4J_TEST_URI")
def test_commit_impact_end_to_end(tmp_path, monkeypatch):
    import graph
    import indexer

    monkeypatch.setenv("NEO4J_URI", URI)
    monkeypatch.setenv("NEO4J_PASSWORD", PASSWORD)
    graph.close_driver()
    graph.setup_schema()
    graph.clear_graph()

    repo = tmp_path / "demo"
    _write(repo, V1)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
    try:
        # an unindexed repo must be refused, not reported as "everything added"
        with pytest.raises(RuntimeError, match="not indexed"):
            impact.analyze(str(repo), repo="demo", head="HEAD", base="HEAD")
        indexer.index_repo(str(repo), repo="demo", stages=("parse",))
        v1 = impact.git_head(str(repo))
        assert impact.indexed_commit("demo") == v1

        _write(repo, {"core.py": V2_CORE, "README.txt": "demo v2\n"})
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2")

        # base defaults to the indexed commit
        r = impact.analyze(str(repo), repo="demo")
        assert r.base == v1 and not any("indexed at" in w for w in r.warnings)

        assert {(c.id, c.change) for c in r.changed} == {
            ("core.legacy", "removed"), ("core.helper", "modified"),
            ("core.fresh", "added"),
        }
        dist = {a.id: a.distance for a in r.affected}
        assert dist == {"core.compute": 1, "api.handler": 2,
                        "tests.test_core.test_compute": 2, "api.old_handler": 1}
        handler = next(a for a in r.affected if a.id == "api.handler")
        assert handler.path_to_change == ["api.handler", "core.compute", "core.helper"]
        assert handler.is_entry_point
        assert next(a for a in r.affected if a.id.startswith("tests.")).is_test

        flags = {f["code"]: f for f in r.risk["flags"]}
        assert flags["REMOVED_BUT_STILL_REFERENCED"]["dependents"] == ["api.old_handler"]
        assert "NO_TESTS_IN_BLAST_RADIUS" not in flags
        assert any("README.txt" in w for w in r.warnings)  # unsupported, but surfaced
        assert any(i["id"] == "api" for i in r.importers)  # api imports core

        # persisted, and round-trips intact
        again = impact.load_report(r.id)
        assert again is not None and again.to_dict() == r.to_dict()
        assert "core.helper" in impact.to_markdown(r)
    finally:
        graph.clear_graph()
        graph.close_driver()
