"""Shared data model for all language extractors.

An extractor's job is to turn ONE source file into a `ParsedFile`: the Symbols it
defines plus *raw, unresolved* references. Cross-file resolution happens later, in
`parsing/__init__.py`, so extractors stay isolated and language-specific.

Every extractor module must expose:
    LANGUAGE     : str                    e.g. "csharp"
    EXTENSIONS   : tuple[str, ...]        e.g. (".cs",)
    parse_file(path, repo_root)  -> ParsedFile
    resolve_call(caller_id, callee, is_self, pf, index) -> str | None
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Symbol:
    id: str
    kind: str  # file | class | interface | function | method | page | proc | table
    name: str
    path: str
    start_line: int
    end_line: int
    language: str
    signature: str = ""
    source: str = ""
    summary: str = ""
    # --- migration metadata (populated by the C#/aspx extractors) -------------
    is_webforms: bool = False
    legacy_apis: list[str] = field(default_factory=list)
    sql_procs: list[str] = field(default_factory=list)


@dataclass
class Edge:
    src: str
    dst: str
    type: str  # CONTAINS | IMPORTS | CALLS | INHERITS | HANDLES | EXECUTES
    count: int = 1


@dataclass
class ParsedFile:
    """One file's symbols plus its unresolved references."""

    symbols: list[Symbol]
    language: str = ""
    # explicit parent->child pairs, recorded where nesting is actually known
    contains: list[tuple[str, str]] = field(default_factory=list)
    # caller_id -> [(callee_name, is_self_call, receiver_text)]
    raw_calls: dict[str, list[tuple[str, bool, str]]] = field(default_factory=dict)
    # type_id -> [base type names]
    raw_inherits: dict[str, list[str]] = field(default_factory=dict)
    # simple/aliased name -> dotted target
    import_map: dict[str, str] = field(default_factory=dict)
    # dotted modules/namespaces this file imports
    imported_modules: list[str] = field(default_factory=list)
    # simple local name -> symbol id (file-scope definitions)
    local_defs: dict[str, str] = field(default_factory=dict)
    # container_id -> {member simple name -> member id}
    class_methods: dict[str, dict[str, str]] = field(default_factory=dict)
    # scope_id (type or method) -> {variable name -> declared type name}
    var_types: dict[str, dict[str, str]] = field(default_factory=dict)
    # page_id -> [handler method names]   (WebForms markup wiring)
    raw_handlers: dict[str, list[str]] = field(default_factory=dict)
    # page_id -> code-behind class name from the @Page directive
    codebehind: dict[str, str] = field(default_factory=dict)


@dataclass
class GlobalIndex:
    """Repo-wide lookups available to per-language resolvers."""

    by_id: dict[str, Symbol]
    qualified: dict[str, str]
    file_ids: set[str]
    classes_by_name: dict[str, str]
    methods_by_name: dict[str, list[str]]
    # `pkg.name` -> real id, for names a package __init__ re-exports
    reexports: dict[str, str] = field(default_factory=dict)


# --- tree-sitter helpers shared by extractors -------------------------------


def node_text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def node_lines(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def rel_path(file_path: str, repo_root: str) -> str:
    return os.path.relpath(file_path, repo_root).replace(os.sep, "/")


def read_bytes(file_path: str) -> bytes:
    with open(file_path, "rb") as f:
        return f.read()


def unique_id(base: str, taken: set[str]) -> str:
    """Disambiguate a colliding id (C# overloads) without hurting the common case."""
    if base not in taken:
        taken.add(base)
        return base
    n = 1
    while f"{base}#{n}" in taken:
        n += 1
    out = f"{base}#{n}"
    taken.add(out)
    return out


def to_dict(sym: Symbol) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(sym)
