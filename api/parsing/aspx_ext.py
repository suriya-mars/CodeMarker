"""ASP.NET WebForms markup extractor (.aspx / .ascx / .master).

Deliberately regex-based, not tree-sitter. `.aspx` is not HTML — it carries
directives (`<%@ Page %>`), data-binding (`<%# %>`), and server blocks (`<% %>`)
that break an HTML parser. The three things we need are unambiguous in the raw
text, so a parser buys nothing here.

This extractor exists to fix the single biggest structural problem in a WebForms
graph (PRODUCTION-DOTNET.md §5.1): **event handlers have no C# callers.**
`btnSave_Click` is invoked by the ASP.NET runtime via markup wiring, so a pure C#
parser sees an orphan. Without HANDLES edges the graph is disconnected at exactly
the points where migration work happens.
"""

from __future__ import annotations

import os
import re

from .base import GlobalIndex, ParsedFile, Symbol, read_bytes, rel_path

LANGUAGE = "aspx"
EXTENSIONS = (".aspx", ".ascx", ".master")

# <%@ Page ... Inherits="Acme.Web.Orders.OrderList" CodeBehind="OrderList.aspx.cs" %>
_DIRECTIVE = re.compile(r"<%@\s*(Page|Control|Master)\b(?P<attrs>[^%]*)%>", re.I | re.S)
_INHERITS = re.compile(r'\bInherits\s*=\s*"([^"]+)"', re.I)
# OnClick="btnSave_Click", OnItemDataBound="grid_Bound", …
_HANDLER = re.compile(r'\bOn([A-Z]\w*)\s*=\s*"([^"\s]+)"')
# <%@ Register TagPrefix="uc" Src="~/Controls/Foo.ascx" %>
_REGISTER_SRC = re.compile(r'<%@\s*Register\b[^%]*\bSrc\s*=\s*"([^"]+)"', re.I)
# <script src="~/Scripts/orders.js"></script>  /  <link href="~/Content/site.css">
_SCRIPT_SRC = re.compile(r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
_LINK_HREF = re.compile(r'<link\b[^>]*\bhref\s*=\s*["\']([^"\']+\.css)["\']', re.I)
# inline <script> ... </script> blocks
_INLINE_JS = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.I | re.S)
# __doPostBack('ctl00$btnSave', '')
_POSTBACK = re.compile(r"__doPostBack\s*\(\s*['\"]([^'\"]+)['\"]")
# server endpoints reached from inline script
_ENDPOINT = re.compile(r"[\w/~.\-]*\.(?:asmx|ashx)(?:/\w+)?", re.I)
# jquery-1.9.1.min.js / bootstrap-3.3.7.css — the VERSION is the migration signal
_LIB_VERSION = re.compile(
    r"\b(jquery(?:-ui)?|bootstrap|knockout|angular|modernizr|prototype)"
    r"[.\-]?(\d+(?:\.\d+)*)?", re.I)
# ID="btnSave" ... OnClick="btnSave_Click"  (attribute order varies)
_CONTROL_ID = re.compile(r'\bID\s*=\s*"([^"]+)"', re.I)
_TAG = re.compile(r"<(asp:[\w.]+)\b([^>]*)>", re.I)
# controls with no .NET Core equivalent
_LEGACY_CONTROLS = {
    "asp:updatepanel", "asp:scriptmanager", "asp:scriptmanagerproxy",
    "asp:timer", "asp:sqldatasource", "asp:objectdatasource",
    "asp:accessdatasource", "asp:menu", "asp:treeview", "asp:xml",
}

# Handlers the runtime calls by convention — never wired in markup.
LIFECYCLE = {
    "Page_Init",
    "Page_Load",
    "Page_PreInit",
    "Page_PreRender",
    "Page_LoadComplete",
    "Page_Unload",
    "Page_Error",
    "Page_DataBind",
    "OnInit",
    "OnLoad",
    "OnPreRender",
    "Application_Start",
    "Application_End",
    "Session_Start",
    "Session_End",
}


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    raw = read_bytes(file_path).decode("utf-8", "replace")
    path = rel_path(file_path, repo_root)

    filename = os.path.basename(file_path)
    page = Symbol(
        id=path,
        kind="page",
        # Stem, not filename: Lucene's standard analyzer keeps "OrderList.aspx"
        # as ONE token, so a search for "OrderList" would never match it. People
        # say "the OrderList page"; the extension stays in `signature`/`path`.
        name=os.path.splitext(filename)[0],
        path=path,
        start_line=1,
        end_line=raw.count("\n") + 1,
        language=LANGUAGE,
        signature=filename,
        source=raw,
        is_webforms=True,
    )
    pf = ParsedFile(symbols=[page], language=LANGUAGE)

    directive = _DIRECTIVE.search(raw)
    if directive:
        inherits = _INHERITS.search(directive.group("attrs"))
        if inherits:
            pf.codebehind[path] = inherits.group(1).split(",")[0].strip()

    handlers = sorted({m.group(2) for m in _HANDLER.finditer(raw)})
    if handlers:
        pf.raw_handlers[path] = handlers

    # control id -> handler, so JS __doPostBack('ctl00$btnSave') can be resolved
    # to the server method that actually runs.
    mapping: dict[str, str] = {}
    for tag in _TAG.finditer(raw):
        attrs = tag.group(2)
        cid = _CONTROL_ID.search(attrs)
        evt = _HANDLER.search(attrs)
        if cid and evt:
            mapping[cid.group(1)] = evt.group(2)
    if mapping:
        pf.control_handlers[path] = mapping

    # user controls, scripts and stylesheets this page pulls in. These land in
    # imported_modules so the shared IMPORTS resolution links them — no new edge
    # type needed, and it only fires when the target is actually indexed.
    assets: list[str] = []
    for pattern in (_REGISTER_SRC, _SCRIPT_SRC, _LINK_HREF):
        assets += [_normalise(m.group(1)) for m in pattern.finditer(raw)]
    pf.imported_modules.extend(a for a in assets if a)

    libs, legacy = _detect_libraries(assets), set()

    # inline <script> blocks: the client/server bridge lives here as often as in
    # a .js file, and it is invisible to every other extractor.
    inline = "\n".join(m.group(1) for m in _INLINE_JS.finditer(raw))
    if inline:
        targets = sorted({m.group(1).split("$")[-1] for m in _POSTBACK.finditer(inline)})
        if targets:
            pf.postback_targets[path] = targets
            legacy.add("__doPostBack")
        page.ajax_endpoints = sorted(set(_ENDPOINT.findall(inline)))
        if "$(" in inline or "jQuery" in inline:
            legacy.add("jQuery")

    for tag in _TAG.finditer(raw):
        if tag.group(1).lower() in _LEGACY_CONTROLS:
            legacy.add(tag.group(1))

    page.legacy_libs = libs
    page.legacy_apis = sorted(legacy)
    return pf


def _normalise(ref: str) -> str:
    """`~/Scripts/orders.js` -> `Scripts/orders.js`; drop absolute URLs (CDNs)."""
    ref = ref.strip().split("?")[0].replace("\\", "/")
    if ref.startswith(("http://", "https://", "//")):
        return ""
    return ref.lstrip("~/").lstrip("/")


def _detect_libraries(refs: list[str]) -> list[str]:
    """Library + version from asset filenames.

    We never index vendored libraries — thousands of meaningless symbols at real
    summarisation cost. The VERSION is the part that matters for migration
    (jQuery 1.x and Bootstrap 3 both force rework), so record just that.
    """
    found: set[str] = set()
    for ref in refs:
        name = os.path.basename(ref)
        for lib, ver in _LIB_VERSION.findall(name):
            found.add(f"{lib.lower()}-{ver}" if ver else lib.lower())
    return sorted(found)


def resolve_call(
    caller_id, callee_name, is_self, receiver, pf: ParsedFile, index: GlobalIndex
):
    """Markup declares no calls of its own — HANDLES edges are built by the
    orchestrator from `raw_handlers` + `codebehind`."""
    return None


def resolve_handlers(pf: ParsedFile, index: GlobalIndex) -> list[tuple[str, str]]:
    """(page_id, method_id) pairs: markup-wired handlers plus lifecycle methods."""
    out: list[tuple[str, str]] = []
    for page_id, class_name in pf.codebehind.items():
        class_id = (
            class_name
            if class_name in index.by_id
            else index.classes_by_name.get(class_name.split(".")[-1])
        )
        if not class_id:
            continue
        # explicitly wired in markup
        for handler in pf.raw_handlers.get(page_id, []):
            cand = f"{class_id}.{handler}"
            if cand in index.by_id:
                out.append((page_id, cand))
        # invoked by the runtime, by convention
        for name in LIFECYCLE:
            cand = f"{class_id}.{name}"
            if cand in index.by_id:
                out.append((page_id, cand))
        out.append((page_id, class_id))  # the page owns its code-behind type
    return out
