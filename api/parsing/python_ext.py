"""Python extractor: source file -> Symbols + raw references.

Resolution tiers for CALLS (MVP, unchanged from Phase 1):
  1. self.method()      -> the enclosing class's methods
  2. helper()           -> a same-file top-level definition
  3. imported name      -> a dotted target that exists as a Symbol
Unresolved calls are dropped, never guessed (CLAUDE.md §2.2).
"""

from __future__ import annotations

import os

from tree_sitter_languages import get_parser

from .base import (
    GlobalIndex,
    ParsedFile,
    Symbol,
    node_lines,
    node_text,
    read_bytes,
    rel_path,
)

LANGUAGE = "python"
EXTENSIONS = (".py",)


def module_path(file_path: str, repo_root: str) -> str:
    """`<repo>/pkg/auth.py` -> `pkg.auth`; `__init__.py` -> the package dir."""
    rel = os.path.relpath(file_path, repo_root)
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = [p for p in rel.split(os.sep) if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    src = read_bytes(file_path)
    root = get_parser(LANGUAGE).parse(src).root_node

    module = module_path(file_path, repo_root)
    path = rel_path(file_path, repo_root)

    file_sym = Symbol(
        id=module,
        kind="file",
        name=os.path.basename(file_path),
        path=path,
        start_line=1,
        end_line=root.end_point[0] + 1,
        language=LANGUAGE,
    )
    pf = ParsedFile(symbols=[file_sym], language=LANGUAGE)

    _extract_imports(
        root, src, module, pf,
        is_package=os.path.basename(file_path) == "__init__.py",
    )

    for child in root.named_children:
        target = child
        if child.type == "decorated_definition":
            target = child.child_by_field_name("definition")
            if target is None:
                continue
        if target.type == "function_definition":
            _add_function(
                target, src, module, path, pf, kind="function", parent_id=module
            )
        elif target.type == "class_definition":
            _add_class(target, src, module, path, pf, file_id=module)

    return pf


def resolve_relative(base: str, module: str, is_package: bool = False) -> str:
    """`.noise_schedule` inside `pkg.sub.mod` -> `pkg.sub.noise_schedule`.

    Relative imports are ubiquitous in real packages, and without this every one
    of them silently produced a dotted name (`.noise_schedule.cosine_schedule`)
    that could never match a Symbol id — dropping both the IMPORTS edge and every
    CALLS edge through it. Measured on a real repo, this accounted for the
    largest single group of unresolved calls.
    """
    dots = len(base) - len(base.lstrip("."))
    if not dots:
        return base
    rest = base[dots:]
    # A package's __init__.py has the PACKAGE as its module id, so its own
    # package is itself — stripping the last component would walk up one level
    # too far and turn `.noise_schedule` into `noise_schedule` instead of
    # `pkg.noise_schedule`.
    parts = module.split(".") if is_package else module.split(".")[:-1]
    if dots > 1:  # each extra dot walks one package up
        parts = parts[: max(0, len(parts) - (dots - 1))]
    return ".".join([p for p in (*parts, rest) if p])


def _extract_imports(root, src: bytes, module: str, pf: ParsedFile,
                     is_package: bool = False) -> None:
    for node in root.named_children:
        if node.type == "import_statement":
            for name_node in node.named_children:
                _record_plain_import(name_node, src, pf)
        elif node.type == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            base = node_text(mod_node, src) if mod_node else ""
            if base.startswith("."):
                base = resolve_relative(base, module, is_package)
            if base:
                pf.imported_modules.append(base)
            for child in node.named_children:
                if child is mod_node:
                    continue
                _record_from_import(child, src, base, pf)


def _record_plain_import(name_node, src: bytes, pf: ParsedFile) -> None:
    if name_node.type == "aliased_import":
        target = name_node.child_by_field_name("name")
        alias = name_node.child_by_field_name("alias")
        if target is None or alias is None:
            return
        dotted = node_text(target, src)
        pf.imported_modules.append(dotted)
        pf.import_map[node_text(alias, src)] = dotted
    elif name_node.type in ("dotted_name", "identifier"):
        dotted = node_text(name_node, src)
        pf.imported_modules.append(dotted)
        pf.import_map[dotted.split(".")[0]] = dotted.split(".")[0]


def _record_from_import(child, src: bytes, base: str, pf: ParsedFile) -> None:
    if child.type == "aliased_import":
        target = child.child_by_field_name("name")
        alias = child.child_by_field_name("alias")
        if target is None or alias is None:
            return
        name = node_text(target, src)
        pf.import_map[node_text(alias, src)] = f"{base}.{name}" if base else name
    elif child.type in ("dotted_name", "identifier"):
        name = node_text(child, src)
        pf.import_map[name.split(".")[-1]] = f"{base}.{name}" if base else name


def _add_function(node, src, module, path, pf, *, kind, parent_id, class_name=None):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = node_text(name_node, src)
    sym_id = f"{module}.{class_name}.{name}" if class_name else f"{module}.{name}"
    start, end = node_lines(node)

    params = node.child_by_field_name("parameters")
    signature = f"{name}{node_text(params, src)}" if params else f"{name}()"

    pf.symbols.append(
        Symbol(
            id=sym_id,
            kind=kind,
            name=name,
            path=path,
            start_line=start,
            end_line=end,
            language=LANGUAGE,
            signature=signature,
            source=node_text(node, src),
        )
    )
    pf.contains.append((parent_id, sym_id))
    if kind == "function":
        pf.local_defs[name] = sym_id

    body = node.child_by_field_name("body")
    if body is not None:
        pf.raw_calls[sym_id] = _collect_calls(body, src)
    return sym_id


def _add_class(node, src, module, path, pf, *, file_id):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = node_text(name_node, src)
    sym_id = f"{module}.{name}"
    start, end = node_lines(node)

    pf.symbols.append(
        Symbol(
            id=sym_id,
            kind="class",
            name=name,
            path=path,
            start_line=start,
            end_line=end,
            language=LANGUAGE,
            source=node_text(node, src),
        )
    )
    pf.contains.append((file_id, sym_id))
    pf.local_defs[name] = sym_id

    supers = node.child_by_field_name("superclasses")
    bases = [
        node_text(arg, src).split(".")[-1]
        for arg in (supers.named_children if supers is not None else [])
        if arg.type in ("identifier", "attribute")
    ]
    if bases:
        pf.raw_inherits[sym_id] = bases

    methods: dict[str, str] = {}
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.named_children:
            target = child
            if child.type == "decorated_definition":
                target = child.child_by_field_name("definition")
            if target is not None and target.type == "function_definition":
                mname_node = target.child_by_field_name("name")
                if mname_node is None:
                    continue
                mname = node_text(mname_node, src)
                _add_function(
                    target,
                    src,
                    module,
                    path,
                    pf,
                    kind="method",
                    parent_id=sym_id,
                    class_name=name,
                )
                methods[mname] = f"{module}.{name}.{mname}"
    pf.class_methods[sym_id] = methods
    return sym_id


def _collect_calls(node, src: bytes) -> list[tuple[str, bool, str]]:
    """Calls in this body, not descending into nested def/class boundaries."""
    calls: list[tuple[str, bool, str]] = []

    def walk(n):
        for c in n.named_children:
            if c.type in ("function_definition", "class_definition"):
                continue
            if c.type == "call":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    if fn.type == "identifier":
                        calls.append((node_text(fn, src), False, ""))
                    elif fn.type == "attribute":
                        obj = fn.child_by_field_name("object")
                        attr = fn.child_by_field_name("attribute")
                        if attr is not None:
                            recv = node_text(obj, src) if obj is not None else ""
                            calls.append((node_text(attr, src), recv == "self", recv))
            walk(c)

    walk(node)
    return calls


def resolve_call(
    caller_id, callee_name, is_self, receiver, pf: ParsedFile, index: GlobalIndex
):
    if is_self:
        class_id = caller_id.rsplit(".", 1)[0]
        methods = pf.class_methods.get(class_id)
        return methods.get(callee_name) if methods else None
    if callee_name in pf.local_defs:
        return pf.local_defs[callee_name]
    target = pf.import_map.get(callee_name)
    if target and target in index.qualified:
        return index.qualified[target]
    # the name may be re-exported by a package __init__
    if target and target in index.reexports:
        return index.reexports[target]
    return None
