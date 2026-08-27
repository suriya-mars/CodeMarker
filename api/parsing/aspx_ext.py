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

    # user controls this page renders
    for m in _REGISTER_SRC.finditer(raw):
        src = m.group(1).lstrip("~/").replace("\\", "/")
        pf.imported_modules.append(src)

    return pf


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
