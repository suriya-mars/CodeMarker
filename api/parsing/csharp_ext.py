"""C# extractor: source file -> Symbols + raw references.

Tuned for WebForms -> .NET Core migration work, so it captures three things a
generic C# parser would not:

  * `is_webforms`  — the type derives from Page / UserControl / MasterPage
  * `legacy_apis`  — Session / ViewState / HttpContext / … usage, the real
                     migration blockers (PRODUCTION-DOTNET.md §5.5)
  * `sql_procs`    — stored procedure names reached from this method, which is
                     what later becomes the EXECUTES edge (§5.4)

Id scheme:  file  -> relative path        (C# files do not map 1:1 to namespaces)
            type  -> Namespace.TypeName
            member-> Namespace.TypeName.MemberName   (`#n` suffix on overloads)
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

LANGUAGE = "csharp"
EXTENSIONS = (".cs",)

# Base types that mark a WebForms artifact.
_WEBFORMS_BASES = {
    "Page",
    "UserControl",
    "MasterPage",
    "CompositeControl",
    "WebControl",
}

# APIs with no direct .NET Core equivalent — the migration blockers.
LEGACY_APIS = {
    "Session",
    "ViewState",
    "HttpContext",
    "Server",
    "Request",
    "Response",
    "Application",
    "IsPostBack",
    "ClientScript",
    "ScriptManager",
    "HttpRuntime",
    "Profile",
    "MembershipUser",
    "Membership",
    "Roles",
    "FormsAuthentication",
    "__doPostBack",
    "SqlDataSource",
    "ObjectDataSource",
}

_PROC_PREFIX = re.compile(r"^(usp|sp|proc|prc)[_A-Z]\w*$", re.IGNORECASE)
_DAPPER = {
    "Query",
    "QueryAsync",
    "QueryFirst",
    "QueryFirstAsync",
    "QuerySingle",
    "QuerySingleAsync",
    "QueryFirstOrDefault",
    "QueryFirstOrDefaultAsync",
    "Execute",
    "ExecuteAsync",
    "ExecuteScalar",
    "ExecuteScalarAsync",
}
_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "struct_declaration": "class",
    "record_declaration": "class",
}
_MEMBER_DECLS = {
    "method_declaration": "method",
    "constructor_declaration": "method",
    "property_declaration": "method",
}


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    src = read_bytes(file_path)
    root = get_parser("c_sharp").parse(src).root_node

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

    _extract_usings(root, src, pf)
    _walk_namespaces(root, src, "", path, pf, taken)
    return pf


def _extract_usings(root, src: bytes, pf: ParsedFile) -> None:
    for node in root.named_children:
        if node.type != "using_directive":
            continue
        name = node.child_by_field_name("name")
        if name is None:
            continue
        dotted = node_text(name, src)
        pf.imported_modules.append(dotted)
        alias = node.child_by_field_name("alias")
        if alias is not None:
            pf.import_map[node_text(alias, src).rstrip("= ").strip()] = dotted
        else:
            pf.import_map[dotted.split(".")[-1]] = dotted


def _walk_namespaces(node, src, ns, path, pf, taken) -> None:
    """Descend through namespace declarations, emitting types at each level."""
    for child in node.named_children:
        if child.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
            name_node = child.child_by_field_name("name")
            inner = (
                f"{ns}.{node_text(name_node, src)}"
                if ns and name_node
                else (node_text(name_node, src) if name_node else ns)
            )
            body = child.child_by_field_name("body") or child
            _walk_namespaces(body, src, inner, path, pf, taken)
        elif child.type in _TYPE_DECLS:
            _add_type(child, src, ns, path, pf, taken)
        elif child.type == "declaration_list":
            _walk_namespaces(child, src, ns, path, pf, taken)


def _add_type(node, src, ns, path, pf, taken) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(name_node, src)
    type_id = f"{ns}.{name}" if ns else name
    start, end = node_lines(node)

    bases = _base_names(node, src)
    is_webforms = any(b in _WEBFORMS_BASES for b in bases)

    pf.symbols.append(
        Symbol(
            id=type_id,
            kind=_TYPE_DECLS[node.type],
            name=name,
            path=path,
            start_line=start,
            end_line=end,
            language=LANGUAGE,
            signature=f"{name}{' : ' + ', '.join(bases) if bases else ''}",
            source=node_text(node, src),
            is_webforms=is_webforms,
        )
    )
    pf.contains.append((pf.symbols[0].id, type_id))
    pf.local_defs[name] = type_id
    if bases:
        pf.raw_inherits[type_id] = bases

    members: dict[str, str] = {}
    body = node.child_by_field_name("body")
    if body is not None:
        pf.var_types[type_id] = _collect_var_types(body, src)
    for child in body.named_children if body is not None else []:
        if child.type in _TYPE_DECLS:  # nested type
            _add_type(child, src, type_id, path, pf, taken)
        elif child.type in _MEMBER_DECLS:
            mid = _add_member(child, src, type_id, path, pf, taken, is_webforms)
            if mid:
                members.setdefault(
                    node_text(child.child_by_field_name("name"), src), mid
                )
    pf.class_methods[type_id] = members


def _add_member(node, src, type_id, path, pf, taken, is_webforms) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = node_text(name_node, src)
    sym_id = unique_id(f"{type_id}.{name}", taken)
    start, end = node_lines(node)

    params = node.child_by_field_name("parameters")
    signature = f"{name}{node_text(params, src)}" if params is not None else name

    body = node.child_by_field_name("body")
    legacy, procs = ([], [])
    if body is not None:
        pf.raw_calls[sym_id] = _collect_calls(body, src)
        pf.var_types[sym_id] = _collect_var_types(body, src)
        legacy = _collect_legacy(body, src)
        procs = _collect_procs(body, src)

    pf.symbols.append(
        Symbol(
            id=sym_id,
            kind="method",
            name=name,
            path=path,
            start_line=start,
            end_line=end,
            language=LANGUAGE,
            signature=signature,
            source=node_text(node, src),
            is_webforms=is_webforms,
            legacy_apis=legacy,
            sql_procs=procs,
        )
    )
    pf.contains.append((type_id, sym_id))
    return sym_id


def _collect_var_types(node, src: bytes) -> dict[str, str]:
    """Map variable/field name -> declared type name, read from the declaration.

    This is what lets `_svc.Save()` resolve when `_svc` is declared
    `IOrderService` — reading the declared type, not guessing from the name.
    `var x = new Foo()` falls back to the initializer's concrete type.
    """
    out: dict[str, str] = {}

    def walk(n):
        if n.type == "variable_declaration":
            ty = n.child_by_field_name("type")
            tname = (
                node_text(ty, src).split("<")[0].split(".")[-1]
                if ty is not None
                else ""
            )
            for d in n.named_children:
                if d.type != "variable_declarator":
                    continue
                nm = d.child_by_field_name("name")
                if nm is None:
                    continue
                resolved = tname
                if tname in ("var", ""):
                    for sub in _descendants(d):
                        if sub.type == "object_creation_expression":
                            ct = sub.child_by_field_name("type")
                            if ct is not None:
                                resolved = (
                                    node_text(ct, src).split("<")[0].split(".")[-1]
                                )
                            break
                if resolved and resolved != "var":
                    out[node_text(nm, src)] = resolved
        for c in n.named_children:
            walk(c)

    walk(node)
    return out


def _descendants(node):
    for c in node.named_children:
        yield c
        yield from _descendants(c)


def _base_names(node, src) -> list[str]:
    for child in node.named_children:
        if child.type == "base_list":
            return [
                node_text(b, src).split("<")[0].split(".")[-1]
                for b in child.named_children
                if b.type in ("identifier", "qualified_name", "generic_name")
            ]
    return []


def _collect_calls(node, src: bytes) -> list[tuple[str, bool, str]]:
    """Invocations in this body. `is_self` marks `this.X()` and bare `X()` —
    in C# an unqualified call is an implicit `this.` member call."""
    calls: list[tuple[str, bool, str]] = []

    def walk(n):
        for c in n.named_children:
            if c.type in _TYPE_DECLS or c.type == "local_function_statement":
                continue
            if c.type == "invocation_expression":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    if fn.type == "identifier":
                        calls.append(
                            (node_text(fn, src), True, "this")
                        )  # implicit this
                    elif fn.type == "member_access_expression":
                        recv = fn.child_by_field_name("expression")
                        nm = fn.child_by_field_name("name")
                        if nm is not None:
                            rtext = node_text(recv, src) if recv is not None else ""
                            calls.append(
                                (node_text(nm, src), rtext in ("this", "base"), rtext)
                            )
            elif c.type == "object_creation_expression":
                ty = c.child_by_field_name("type")
                if ty is not None:
                    calls.append(
                        (node_text(ty, src).split("<")[0].split(".")[-1], False, "")
                    )
            walk(c)

    walk(node)
    return calls


def _collect_legacy(node, src: bytes) -> list[str]:
    found: set[str] = set()

    def walk(n):
        if n.type == "identifier":
            txt = node_text(n, src)
            if txt in LEGACY_APIS:
                found.add(txt)
        for c in n.named_children:
            walk(c)

    walk(node)
    return sorted(found)


def _collect_procs(node, src: bytes) -> list[str]:
    """Stored procedure names referenced from this body.

    Contextual detection (SqlCommand ctor, CommandText assignment, Dapper call)
    plus a conservative `usp_`/`sp_` prefix fallback. Dynamic SQL is NOT guessed.
    """
    found: set[str] = set()

    def lit(n) -> str | None:
        if n is not None and n.type in ("string_literal", "verbatim_string_literal"):
            return node_text(n, src).strip('@"').strip('"')
        return None

    def first_string_arg(call_node) -> str | None:
        args = call_node.child_by_field_name("arguments")
        for a in args.named_children if args is not None else []:
            for cand in (a, *a.named_children):
                v = lit(cand)
                if v:
                    return v
        return None

    def walk(n):
        if n.type == "object_creation_expression":
            ty = n.child_by_field_name("type")
            if ty is not None and "SqlCommand" in node_text(ty, src):
                v = first_string_arg(n)
                if v:
                    found.add(v)
        elif n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is not None and node_text(left, src).endswith("CommandText"):
                v = lit(right)
                if v:
                    found.add(v)
        elif n.type == "invocation_expression":
            fn = n.child_by_field_name("function")
            nm = (
                fn.child_by_field_name("name")
                if fn is not None and fn.type == "member_access_expression"
                else None
            )
            base = node_text(nm, src).split("<")[0] if nm is not None else ""
            if base in _DAPPER:
                v = first_string_arg(n)
                if v:
                    found.add(v)
        elif n.type in ("string_literal", "verbatim_string_literal"):
            v = lit(n)
            if v and _PROC_PREFIX.match(v):
                found.add(v)
        for c in n.named_children:
            walk(c)

    walk(node)
    return sorted(found)


def resolve_call(
    caller_id, callee_name, is_self, receiver, pf: ParsedFile, index: GlobalIndex
):
    """C# resolution tiers, most-certain first. Only tier 5 infers anything, and
    it fires solely when the name is unique repo-wide."""
    type_id = caller_id.rsplit(".", 1)[0]

    # 1. this.X() / bare X() -> a member of the enclosing type
    members = pf.class_methods.get(type_id)
    if members and callee_name in members:
        return members[callee_name]

    # 2. base.X() -> a member of a declared base type defined in this repo
    for base in pf.raw_inherits.get(type_id, []):
        base_id = pf.import_map.get(base) or index.classes_by_name.get(base)
        if base_id and f"{base_id}.{callee_name}" in index.by_id:
            return f"{base_id}.{callee_name}"

    # 3. receiver.X() where the receiver's type is DECLARED in scope
    #    (`private IOrderService _svc;` -> `_svc.Save()` -> IOrderService.Save)
    if receiver and receiver not in ("this", "base"):
        tname = pf.var_types.get(caller_id, {}).get(receiver) or pf.var_types.get(
            type_id, {}
        ).get(receiver)
        owner = None
        if tname:
            owner = pf.import_map.get(tname) or index.classes_by_name.get(tname)
        elif receiver in index.classes_by_name:  # static call: Helper.Log()
            owner = index.classes_by_name[receiver]
        if owner and f"{owner}.{callee_name}" in index.by_id:
            return f"{owner}.{callee_name}"

    # 4. a type name visible in this file -> the type itself (`new Foo()`)
    owner = pf.local_defs.get(callee_name) or pf.import_map.get(callee_name)
    if owner and owner in index.by_id:
        return owner
    if not is_self and callee_name in index.classes_by_name:
        return index.classes_by_name[callee_name]

    # 5. unambiguous repo-wide method name — the one inference, unique-only
    hits = index.methods_by_name.get(callee_name, [])
    return hits[0] if len(hits) == 1 else None
