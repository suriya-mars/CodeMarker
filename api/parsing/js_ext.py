"""JavaScript extractor, tuned for the WebForms client/server bridge.

In a WebForms app the JavaScript is entangled with the server-side model in ways
a C# parser cannot see:

    orders.js --__doPostBack--> btnSave_Click --> OrderService.Save --> usp_SaveOrder
              ^^^^^^^^^^^^^^^^
              invisible without this extractor, so blast radius under-reports:
              change a handler and the JS that triggers it never shows up.

All of it also needs rewriting for Razor/Blazor, so it is migration work worth
counting. What we extract beyond ordinary functions and calls:

  * `__doPostBack('ctl00$btnSave')`   -> a TRIGGERS edge to the server handler
  * `$.ajax({url: '...asmx/Method'})` -> ajax endpoint dependencies
  * `$('#<%= X.ClientID %>')`         -> bindings to server controls
  * jQuery usage                      -> a migration signal

**Vendored libraries are deliberately NOT indexed.** Adding jquery-1.9.1.min.js
would contribute thousands of meaningless symbols, cost real summarisation money,
and bury your own code in noise. What matters about a library is its VERSION,
which `aspx_ext` records from the script tag.
"""

from __future__ import annotations

import os
import re

from tree_sitter_languages import get_parser

from .base import (
    GlobalIndex,
    ParsedFile,
    Symbol,
    node_lines,
    node_text,
    read_bytes,
    rel_path,
    unique_id,
)

LANGUAGE = "javascript"
EXTENSIONS = (".js",)

# Endpoints a WebForms page talks to: .asmx web services, .ashx handlers,
# and classic page methods.
_ENDPOINT = re.compile(r"[\w/~.\-]*\.(?:asmx|ashx|aspx)(?:/\w+)?", re.I)
_AJAX_FNS = {"ajax", "get", "post", "getJSON", "load"}
_JQUERY_HINTS = ("$", "jQuery")


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    src = read_bytes(file_path)
    root = get_parser(LANGUAGE).parse(src).root_node
    path = rel_path(file_path, repo_root)

    file_sym = Symbol(
        id=path,
        kind="file",
        name=os.path.basename(file_path),
        path=path,
        start_line=1,
        end_line=root.end_point[0] + 1,
        language=LANGUAGE,
    )
    pf = ParsedFile(symbols=[file_sym], language=LANGUAGE)
    taken: set[str] = set()

    _walk(root, src, path, path, pf, taken)

    # File-level rollup, so a page can be assessed without opening every function.
    text = src.decode("utf-8", "replace")
    if any(h in text for h in _JQUERY_HINTS):
        file_sym.legacy_apis = sorted({*file_sym.legacy_apis, "jQuery"})
    if "__doPostBack" in text:
        file_sym.legacy_apis = sorted({*file_sym.legacy_apis, "__doPostBack"})
    return pf


def _walk(node, src, path, parent_id, pf, taken, class_name=None) -> None:
    for child in node.named_children:
        if child.type == "function_declaration":
            _add_callable(child, src, path, parent_id, pf, taken, class_name)
        elif child.type == "class_declaration":
            cid = _add_class(child, src, path, parent_id, pf, taken)
            body = child.child_by_field_name("body")
            if body is not None and cid:
                _walk(body, src, path, cid, pf, taken, class_name=cid)
        elif child.type == "method_definition":
            _add_callable(child, src, path, parent_id, pf, taken, class_name)
        elif child.type == "variable_declarator":
            value = child.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression"):
                _add_callable(child, src, path, parent_id, pf, taken,
                              class_name, body_node=value)
            else:
                _walk(child, src, path, parent_id, pf, taken, class_name)
        else:
            _walk(child, src, path, parent_id, pf, taken, class_name)


def _add_class(node, src, path, parent_id, pf, taken) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = node_text(name_node, src)
    sym_id = unique_id(f"{path}::{name}", taken)
    start, end = node_lines(node)
    pf.symbols.append(
        Symbol(id=sym_id, kind="class", name=name, path=path, start_line=start,
               end_line=end, language=LANGUAGE, source=node_text(node, src))
    )
    pf.contains.append((parent_id, sym_id))
    pf.local_defs[name] = sym_id
    return sym_id


def _add_callable(node, src, path, parent_id, pf, taken, class_name, body_node=None):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = node_text(name_node, src)
    owner = class_name.split("::")[-1] if class_name else None
    sym_id = unique_id(f"{path}::{owner + '.' if owner else ''}{name}", taken)
    start, end = node_lines(node)

    target = body_node or node
    params = target.child_by_field_name("parameters")
    signature = f"{name}{node_text(params, src)}" if params is not None else f"{name}()"

    body = target.child_by_field_name("body") or target
    pf.raw_calls[sym_id] = _collect_calls(body, src)
    endpoints, postbacks = _collect_bridges(body, src)

    pf.symbols.append(
        Symbol(
            id=sym_id, kind="method" if class_name else "function", name=name,
            path=path, start_line=start, end_line=end, language=LANGUAGE,
            signature=signature, source=node_text(node, src),
            sql_procs=[],
            # the API used, not the control ids — those live in postback_targets
            # and become TRIGGERS edges
            legacy_apis=["__doPostBack"] if postbacks else [],
            ajax_endpoints=sorted(set(endpoints)),
        )
    )
    pf.contains.append((parent_id, sym_id))
    if not class_name:
        pf.local_defs[name] = sym_id
    if class_name:
        pf.class_methods.setdefault(class_name, {})[name] = sym_id
    pf.postback_targets[sym_id] = sorted(set(postbacks))
    return sym_id


def _collect_calls(node, src: bytes) -> list[tuple[str, bool, str]]:
    calls: list[tuple[str, bool, str]] = []

    def walk(n):
        for c in n.named_children:
            if c.type in ("function_declaration", "class_declaration"):
                continue
            if c.type == "call_expression":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    if fn.type == "identifier":
                        calls.append((node_text(fn, src), False, ""))
                    elif fn.type == "member_expression":
                        prop = fn.child_by_field_name("property")
                        obj = fn.child_by_field_name("object")
                        if prop is not None:
                            recv = node_text(obj, src) if obj is not None else ""
                            calls.append((node_text(prop, src),
                                          recv == "this", recv[:40]))
            walk(c)

    walk(node)
    return calls


def _collect_bridges(node, src: bytes) -> tuple[list[str], list[str]]:
    """Server endpoints called and postback targets triggered from this body."""
    endpoints: list[str] = []
    postbacks: list[str] = []

    def strip(s: str) -> str:
        return s.strip("'\"`")

    def walk(n):
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            fname = ""
            if fn is not None:
                if fn.type == "identifier":
                    fname = node_text(fn, src)
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    fname = node_text(prop, src) if prop is not None else ""

            if fname == "__doPostBack" and args is not None:
                for a in args.named_children:
                    if a.type in ("string", "template_string"):
                        target = strip(node_text(a, src))
                        if target:
                            # 'ctl00$ctl01$btnSave' -> the control id is the last part
                            postbacks.append(target.split("$")[-1])
                        break
            elif fname in _AJAX_FNS and args is not None:
                endpoints.extend(_ENDPOINT.findall(node_text(args, src)))
        elif n.type in ("string", "template_string"):
            endpoints.extend(_ENDPOINT.findall(node_text(n, src)))
        for c in n.named_children:
            walk(c)

    walk(node)
    return endpoints, postbacks


def resolve_call(caller_id, callee_name, is_self, receiver, pf: ParsedFile,
                 index: GlobalIndex):
    """JS resolution: same-file definitions, then an unambiguous repo-wide name.
    Dynamic dispatch is not guessed at (CLAUDE.md §2.2)."""
    if callee_name in pf.local_defs:
        return pf.local_defs[callee_name]
    for members in pf.class_methods.values():
        if callee_name in members:
            return members[callee_name]
    hits = [i for i in index.methods_by_name.get(callee_name, []) if "::" in i]
    return hits[0] if len(hits) == 1 else None
