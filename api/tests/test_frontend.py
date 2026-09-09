"""JavaScript / CSS / enriched-ASPX extraction.

These close the WebForms client/server gap. Without them the graph stops at the
C# boundary, so changing an event handler never surfaces the JavaScript that
fires it — and blast radius silently under-reports.
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


def test_all_five_languages_coexist():
    symbols, _ = _parse()
    langs = {s.language for s in symbols}
    assert {"csharp", "aspx", "javascript", "css"} <= langs
    assert "javascript" in parsing.LANGUAGES and "css" in parsing.LANGUAGES


def test_javascript_functions_and_classes_extracted():
    by_id = _by_id(_parse()[0])
    assert by_id["Scripts/orders.js::bindOrderGrid"].kind == "function"
    assert by_id["Scripts/orders.js::OrderGrid"].kind == "class"
    assert by_id["Scripts/orders.js::OrderGrid.reload"].kind == "method"


def test_js_internal_calls_resolve():
    calls = _edges(_parse()[1], "CALLS")
    assert ("Scripts/orders.js::bindOrderGrid",
            "Scripts/orders.js::refreshTotals") in calls
    assert ("Scripts/orders.js::OrderGrid.reload",
            "Scripts/orders.js::bindOrderGrid") in calls


def test_triggers_edge_bridges_javascript_to_the_server_handler():
    """The core fix: __doPostBack('ctl00$btnSave') in a .js file reaches
    btnSave_Click in C#, via the page that loads the script."""
    triggers = _edges(_parse()[1], "TRIGGERS")
    assert ("Scripts/orders.js::bindOrderGrid",
            "Acme.Web.Orders.OrderList.btnSave_Click") in triggers


def test_inline_script_in_the_page_also_triggers():
    triggers = _edges(_parse()[1], "TRIGGERS")
    assert ("Web/Orders/OrderList.aspx",
            "Acme.Web.Orders.OrderList.grid_RowDataBound") in triggers


def test_page_references_its_scripts_and_stylesheets():
    imports = _edges(_parse()[1], "IMPORTS")
    assert ("Web/Orders/OrderList.aspx", "Scripts/orders.js") in imports
    assert ("Web/Orders/OrderList.aspx", "Content/site.css") in imports


def test_vendor_libraries_are_not_indexed_but_their_versions_are():
    """Indexing jquery.min.js would add thousands of meaningless symbols at real
    summarisation cost. The VERSION is what matters for migration."""
    symbols, _ = _parse()
    assert not any("jquery" in s.id.lower() for s in symbols)

    page = _by_id(symbols)["Web/Orders/OrderList.aspx"]
    assert "jquery-1.9.1" in page.legacy_libs
    assert "bootstrap-3.3.7" in page.legacy_libs


def test_legacy_controls_and_apis_flagged_on_the_page():
    page = _by_id(_parse()[0])["Web/Orders/OrderList.aspx"]
    assert "asp:UpdatePanel" in page.legacy_apis
    assert "__doPostBack" in page.legacy_apis
    assert "jQuery" in page.legacy_apis


def test_ajax_endpoints_captured():
    by_id = _by_id(_parse()[0])
    assert by_id["Scripts/orders.js::refreshTotals"].ajax_endpoints == [
        "/Services/OrderService.asmx/GetTotal"
    ]


def test_postback_control_ids_do_not_leak_into_legacy_apis():
    """Control ids belong in postback_targets and become TRIGGERS edges;
    legacy_apis records the API used, not the widget name."""
    fn = _by_id(_parse()[0])["Scripts/orders.js::bindOrderGrid"]
    assert fn.legacy_apis == ["__doPostBack"]


def test_css_selectors_extracted_without_pseudo_classes():
    """The grammar emits `class_name` for `:hover` too — recording that as a
    class would be wrong."""
    sheet = _by_id(_parse()[0])["Content/site.css"]
    assert ".order-grid" in sheet.selectors
    assert "#main-panel" in sheet.selectors
    assert not any("hover" in s for s in sheet.selectors)
