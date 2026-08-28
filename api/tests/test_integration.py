"""End-to-end integration: index the WebForms fixture into a real Neo4j, then
exercise every graph tool's Cypher.

Unit tests cover the extractors; this covers the half that only fails at runtime —
the Cypher itself. Skips cleanly when no Neo4j is reachable.

Point it at an instance with:
    NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=… pytest
"""

import os

import pytest

import graph
import indexer
import summarize
import tools

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "webforms_app")
URI = os.environ.get("NEO4J_TEST_URI")
PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD")


def _reachable() -> bool:
    if not URI or not PASSWORD:
        return False
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(URI, auth=("neo4j", PASSWORD))
        driver.verify_connectivity()
        driver.close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="no Neo4j at NEO4J_TEST_URI; set it to run integration tests",
)


@pytest.fixture(scope="module", autouse=True)
def indexed_graph():
    """Index the fixture once into a real Neo4j, with the LLM stubbed out."""
    os.environ["NEO4J_URI"] = URI
    os.environ["NEO4J_PASSWORD"] = PASSWORD
    graph.close_driver()  # force a fresh driver on the test URI

    # deterministic stand-in for the Haiku summary pass
    summarize.summarize_symbol = lambda s: f"{s.kind} {s.name} in {s.path}"
    summarize.summarize_community = lambda name, members: f"cluster of {len(members)}"

    stats = indexer.index_repo(FIXTURE)
    yield stats
    graph.clear_graph()
    graph.close_driver()


def test_index_writes_expected_counts(indexed_graph):
    assert indexed_graph["symbols"] == 19
    rows = graph._run("MATCH (s:Symbol) RETURN count(s) AS n")
    assert rows[0]["n"] == 19


def test_all_edge_types_survive_the_write(indexed_graph):
    """HANDLES is the one that silently vanishes if _EDGE_TYPES is not updated."""
    rows = graph._run("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n ORDER BY t")
    counts = {r["t"]: r["n"] for r in rows}
    for etype in ("CONTAINS", "INHERITS", "CALLS", "HANDLES"):
        assert counts.get(etype, 0) > 0, f"{etype} edges missing from the graph"


def test_find_symbol_fulltext(indexed_graph):
    hits = tools.find_symbol("OrderList page")
    assert hits, "full-text index returned nothing"
    assert any("OrderList" in h["id"] for h in hits)
    assert "community_id" in hits[0], "projection must expose community_id"


def test_find_symbol_kind_filter_accepts_page(indexed_graph):
    hits = tools.find_symbol("OrderList", kind="page")
    assert hits and all(h["kind"] == "page" for h in hits)


def test_get_callers_and_callees(indexed_graph):
    callees = tools.get_callees("Acme.Web.Orders.OrderList.btnSave_Click")
    ids = {c["id"] for c in callees}
    assert "Acme.Services.IOrderService.Save" in ids
    callers = tools.get_callers("Acme.Web.Orders.OrderList.BindGrid")
    assert {c["id"] for c in callers} >= {"Acme.Web.Orders.OrderList.Page_Load"}


def test_get_definition_returns_merged_partial(indexed_graph):
    d = tools.get_definition("Acme.Web.Orders.OrderList")
    assert d["kind"] == "class"
    assert "OrderList.aspx.designer.cs" in d["path"]
    assert "Acme.Web.Orders.OrderList.btnSave_Click" in d["children"]
    assert "Acme.Web.BasePage" in d["inherits"]


def test_blast_radius_reaches_the_page_through_handles(indexed_graph):
    """The WebForms payoff: changing a service method must surface the .aspx page,
    which is only reachable via a HANDLES edge."""
    rows = tools.get_blast_radius("Acme.Services.IOrderService.Save", depth=3)
    ids = {r["id"] for r in rows}
    assert "Acme.Web.Orders.OrderList.btnSave_Click" in ids
    assert "Web/Orders/OrderList.aspx" in ids, "page not reached through HANDLES"


def test_migration_blockers_app_wide_ranking(indexed_graph):
    rows = tools.get_migration_blockers()
    assert rows, "no pages reported as blocked"
    top = rows[0]
    assert top["id"] == "Web/Orders/OrderList.aspx"
    assert set(top["legacy_apis"]) >= {"IsPostBack", "Request", "ViewState"}
    assert top["blocker_methods"] >= 3


def test_migration_blockers_scoped_to_a_symbol(indexed_graph):
    rows = tools.get_migration_blockers("Acme.Web.Orders.OrderList")
    ids = {r["id"] for r in rows}
    assert "Acme.Web.Orders.OrderList.BindGrid" in ids
    assert all(r["legacy_apis"] for r in rows)


def test_unknown_symbol_is_handled(indexed_graph):
    assert "error" in tools.get_definition("does.not.exist")
    assert tools.get_callers("does.not.exist") == []


# --- get_page_brief: the page-by-page migration entry point ------------------


def test_page_brief_assembles_the_full_migration_picture(indexed_graph):
    b = tools.get_page_brief("Web/Orders/OrderList.aspx")
    assert b["page"] == "Web/Orders/OrderList.aspx"
    assert b["is_webforms"] is True
    # handlers are METHODS only — the code-behind class is not a handler
    names = {h["name"] for h in b["handlers"]}
    assert names == {"Page_Load", "btnSave_Click", "grid_RowDataBound"}
    assert all(h["id"].count(".") >= 3 for h in b["handlers"])


def test_page_brief_finds_procs_through_interface_indirection(indexed_graph):
    """`_svc.Save()` resolves to IOrderService.Save, which has no body. The real
    implementation and its stored procedure are only reachable backwards through
    INHERITS — without that hop a DI codebase hides all its data access."""
    b = tools.get_page_brief("Web/Orders/OrderList.aspx")
    assert b["stored_procedures"] == ["usp_SaveOrder"]
    via = [r for r in b["reachable_code"] if r.get("via")]
    assert any(r["id"] == "Acme.Services.OrderService.Save" for r in via)


def test_page_brief_counts_each_blocker_once(indexed_graph):
    """Handlers appear in both the entry and reachable sets; counting naively
    double-counts every blocker and inflates the effort estimate."""
    b = tools.get_page_brief("Web/Orders/OrderList.aspx")
    assert b["migration_blockers"] == {
        "IsPostBack": 1, "Request": 1, "Session": 1, "ViewState": 1
    }


def test_page_brief_accepts_the_codebehind_class_id(indexed_graph):
    assert tools.get_page_brief("Acme.Web.Orders.OrderList")["page"] == (
        "Web/Orders/OrderList.aspx"
    )


def test_page_brief_reports_effort_and_handles_unknown_ids(indexed_graph):
    assert isinstance(tools.get_page_brief("Web/Orders/OrderList.aspx")["effort_hint"], str)
    assert "error" in tools.get_page_brief("no.such.page")
