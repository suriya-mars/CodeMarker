"""C# / WebForms extractor tests against a fixture with known structure.

Covers the four things a generic C# parser gets wrong on WebForms
(PRODUCTION-DOTNET.md §5): partial classes, event-handler wiring, legacy API
detection, and stored-procedure references.
"""

import os

import parsing

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "webforms_app")


def _parse():
    return parsing.parse_repo(FIXTURE)


def _by_id(symbols):
    return {s.id: s for s in symbols}


def _edges(edges, etype):
    return {(e.src, e.dst) for e in edges if e.type == etype}


def test_symbols_and_kinds():
    symbols, _ = _parse()
    by_id = _by_id(symbols)
    assert by_id["Acme.Web.Orders.OrderList"].kind == "class"
    assert by_id["Acme.Services.IOrderService"].kind == "interface"
    assert by_id["Acme.Services.OrderService.Save"].kind == "method"
    assert by_id["Web/Orders/OrderList.aspx"].kind == "page"
    assert by_id["Acme.Web.Orders.OrderList.Page_Load"].language == "csharp"


def test_partial_class_merges_into_one_symbol():
    """OrderList.aspx.cs + OrderList.aspx.designer.cs are ONE class."""
    symbols, edges = _parse()
    matches = [s for s in symbols if s.id == "Acme.Web.Orders.OrderList"]
    assert len(matches) == 1, "partial class must not produce duplicate symbols"

    merged = matches[0]
    assert "OrderList.aspx.cs" in merged.path
    assert "OrderList.aspx.designer.cs" in merged.path

    # members from BOTH files hang off the single merged class
    contains = _edges(edges, "CONTAINS")
    assert (
        "Acme.Web.Orders.OrderList",
        "Acme.Web.Orders.OrderList.btnSave_Click",
    ) in contains
    assert (
        "Acme.Web.Orders.OrderList",
        "Acme.Web.Orders.OrderList.InitializeComponent",
    ) in contains
    # and both files claim it
    assert ("Web/Orders/OrderList.aspx.cs", "Acme.Web.Orders.OrderList") in contains
    assert (
        "Web/Orders/OrderList.aspx.designer.cs",
        "Acme.Web.Orders.OrderList",
    ) in contains


def test_handles_edges_connect_markup_to_codebehind():
    """The core WebForms fix: handlers have no C# callers, so without HANDLES
    edges they are orphans (PRODUCTION-DOTNET.md §5.1)."""
    _, edges = _parse()
    handles = _edges(edges, "HANDLES")
    page = "Web/Orders/OrderList.aspx"
    # wired in markup via OnClick= / OnRowDataBound=
    assert (page, "Acme.Web.Orders.OrderList.btnSave_Click") in handles
    assert (page, "Acme.Web.Orders.OrderList.grid_RowDataBound") in handles
    # invoked by the runtime by convention, never named in markup
    assert (page, "Acme.Web.Orders.OrderList.Page_Load") in handles
    # the page owns its code-behind type
    assert (page, "Acme.Web.Orders.OrderList") in handles


def test_inherits_across_files():
    _, edges = _parse()
    inherits = _edges(edges, "INHERITS")
    assert ("Acme.Web.Orders.OrderList", "Acme.Web.BasePage") in inherits
    assert ("Acme.Services.OrderService", "Acme.Services.IOrderService") in inherits


def test_calls_resolution_tiers():
    _, edges = _parse()
    calls = _edges(edges, "CALLS")
    # tier 1: bare BindGrid() is an implicit this.BindGrid()
    assert (
        "Acme.Web.Orders.OrderList.Page_Load",
        "Acme.Web.Orders.OrderList.BindGrid",
    ) in calls
    # tier 2: Audit() is inherited from BasePage
    assert (
        "Acme.Web.Orders.OrderList.btnSave_Click",
        "Acme.Web.BasePage.Audit",
    ) in calls
    # tier 3: _svc is DECLARED IOrderService, so _svc.Save() resolves through it
    assert (
        "Acme.Web.Orders.OrderList.btnSave_Click",
        "Acme.Services.IOrderService.Save",
    ) in calls


def test_legacy_api_detection():
    """These are the migration blockers — no .NET Core equivalent."""
    symbols, _ = _parse()
    by_id = _by_id(symbols)
    assert "IsPostBack" in by_id["Acme.Web.Orders.OrderList.Page_Load"].legacy_apis
    assert "Request" in by_id["Acme.Web.Orders.OrderList.btnSave_Click"].legacy_apis
    assert "ViewState" in by_id["Acme.Web.Orders.OrderList.BindGrid"].legacy_apis
    assert "Session" in by_id["Acme.Web.BasePage.Audit"].legacy_apis
    assert by_id["Acme.Services.OrderService.Save"].legacy_apis == []


def test_stored_procedure_extraction():
    """Both the SqlCommand ctor and the CommandText assignment forms."""
    symbols, _ = _parse()
    by_id = _by_id(symbols)
    assert by_id["Acme.Services.OrderService.Save"].sql_procs == ["usp_SaveOrder"]
    assert by_id["Acme.Services.OrderService.Count"].sql_procs == ["usp_CountOrders"]


def test_webforms_classification():
    symbols, _ = _parse()
    by_id = _by_id(symbols)
    assert by_id["Acme.Web.BasePage"].is_webforms is True  # : Page
    assert by_id["Web/Orders/OrderList.aspx"].is_webforms is True
    assert by_id["Acme.Services.OrderService"].is_webforms is False


def test_python_and_csharp_coexist():
    """Both extractors run over one repo without interfering."""
    py_syms, _ = parsing.parse_repo(
        os.path.join(os.path.dirname(__file__), "fixtures", "sample_repo")
    )
    cs_syms, _ = _parse()
    assert {s.language for s in py_syms} == {"python"}
    # the WebForms fixture is deliberately multi-language: C#, markup, JS and CSS
    assert {"csharp", "aspx"} <= {s.language for s in cs_syms}
    assert {"python", "csharp", "javascript", "css"} <= set(parsing.LANGUAGES)
