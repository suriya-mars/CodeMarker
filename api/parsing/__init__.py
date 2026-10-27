"""Multi-language parsing: repo -> Symbols + edges.

Per CLAUDE.md §10, extractors are per-language and isolated; everything here is
language-agnostic. Adding a language means writing one module and registering it
in `_EXTRACTORS` — nothing else in the pipeline changes.

Two phases, same as Phase 1:
  1. per-file extraction  -> Symbols + RAW, unresolved references
  2. repo-wide resolution -> CONTAINS / IMPORTS / CALLS / INHERITS / HANDLES / EXECUTES

Resolution is shared except for CALLS, which each extractor resolves itself
(`resolve_call`) because scoping rules are the one genuinely language-specific
part. Unresolved references are dropped, never guessed (CLAUDE.md §2.2).
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict

from . import aspx_ext, csharp_ext, css_ext, js_ext, python_ext
from .base import Edge, GlobalIndex, ParsedFile, Symbol, rel_path, to_dict

__all__ = [
    "Edge",
    "GlobalIndex",
    "ParsedFile",
    "Symbol",
    "parse_repo",
    "parse_file",
    "iter_source_files",
    "to_dict",
    "LANGUAGES",
]

_EXTRACTORS = (python_ext, csharp_ext, aspx_ext, js_ext, css_ext)
_BY_EXT = {ext: mod for mod in _EXTRACTORS for ext in mod.EXTENSIONS}
LANGUAGES = tuple(mod.LANGUAGE for mod in _EXTRACTORS)

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "bin",
    "obj",
    "packages",
    ".vs",
    ".idea",
    "dist",
    "build",
    "TestResults",
}
# Generated code carries no migration signal but doubles the symbol count.
_SKIP_FILE_SUFFIXES = (".g.cs", ".g.i.cs", ".AssemblyInfo.cs", ".Designer.vb",
                       ".min.js", ".min.css", "-min.js", ".bundle.js")
# Vendored front-end libraries. Indexing jquery.js would add thousands of
# meaningless symbols at real summarisation cost and bury your own code; the
# only thing that matters about a library is its VERSION, which aspx_ext records
# from the script tag.
_VENDOR_NAMES = (
    "jquery", "bootstrap", "modernizr", "knockout", "angular", "prototype",
    "respond", "moment", "underscore", "lodash", "popper", "datatables",
    "select2", "chosen", "kendo", "telerik", "devexpress",
)
_VENDOR_DIRS = {"lib", "libs", "vendor", "vendors", "third-party", "thirdparty",
                "bower_components", "jspm_packages", "wwwroot/lib"}


# Hostile-input limits. A repo is untrusted input: a 200MB file or a binary blob
# with a .js extension must not exhaust the parser's memory, or cost one LLM
# summary per junk symbol. Override with MAX_FILE_BYTES.
_DEFAULT_MAX_FILE_BYTES = 1_000_000
_BINARY_SNIFF = 8192


def _max_file_bytes() -> int:
    return int(os.environ.get("MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES))


def inside(path: str, root: str) -> bool:
    """True if `path`, with every symlink resolved, is within `root`."""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def iter_source_files(repo_root: str):
    """Yield every file any registered extractor can handle.

    Raises if the path does not exist. `os.walk` on a missing directory yields
    NOTHING rather than failing, which silently turns a typo (or a host path
    passed into a container) into "0 symbols found" — a wrong answer that looks
    like a real one.

    Symlinks are never followed. A repo is untrusted: `config.aspx ->
    /proc/self/environ` would otherwise index the api container's environment —
    LLM keys and the Neo4j password — into the graph, send it to the LLM, and
    serve it back through get_definition.
    """
    if not os.path.isdir(repo_root):
        raise NotADirectoryError(
            f"{repo_root!r} is not a directory that this process can see"
        )
    for dirpath, dirnames, filenames in os.walk(repo_root):  # followlinks=False
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        parts = {p.lower() for p in dirpath.replace(os.sep, "/").split("/")}
        if parts & _VENDOR_DIRS:
            continue
        for fn in filenames:
            if fn.endswith(_SKIP_FILE_SUFFIXES):
                continue
            low = fn.lower()
            if low.endswith((".js", ".css")) and any(v in low for v in _VENDOR_NAMES):
                continue  # vendored library — see _VENDOR_NAMES
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _BY_EXT:
                continue
            path = os.path.join(dirpath, fn)
            if os.path.islink(path) or not inside(path, repo_root):
                print(f"[parse] skipped symlink {path}")
                continue
            yield path


def _unsafe_reason(file_path: str, repo_root: str) -> str | None:
    """Why a file must not be parsed, or None. Checked for EVERY parse —
    full index and impact re-parse alike — so no caller can bypass it."""
    if os.path.islink(file_path) or not inside(file_path, repo_root):
        return "symlink or outside the repo"
    size = os.path.getsize(file_path)
    if size > _max_file_bytes():
        return f"{size:,} bytes exceeds MAX_FILE_BYTES"
    with open(file_path, "rb") as f:
        if b"\0" in f.read(_BINARY_SNIFF):
            return "binary content"
    return None


def parse_file(file_path: str, repo_root: str) -> ParsedFile | None:
    ext = os.path.splitext(file_path)[1].lower()
    mod = _BY_EXT.get(ext)
    if mod is None:
        return None
    reason = _unsafe_reason(file_path, repo_root)
    if reason:
        print(f"[parse] skipped {file_path}: {reason}")
        return None
    return mod.parse_file(file_path, repo_root)


# --- phase 2: repo-wide resolution ------------------------------------------


def _collect_symbols(
    parsed: list[ParsedFile],
) -> tuple[list[Symbol], dict[str, Symbol]]:
    """Flatten symbols, merging C# partial types and disambiguating cross-file
    overloads. Any id we rename is remapped in that file's `contains`/`raw_calls`
    so the edges still point at the right node."""
    by_id: dict[str, Symbol] = {}
    order: list[str] = []
    for pf in parsed:
        remap: dict[str, str] = {}
        for sym in pf.symbols:
            existing = by_id.get(sym.id)
            if existing is None:
                by_id[sym.id] = sym
                order.append(sym.id)
            elif sym.kind in ("class", "interface"):
                _merge_partial(existing, sym)  # partial class -> one node
            else:
                new_id = _next_free(sym.id, by_id)  # cross-file overload
                remap[sym.id] = new_id
                sym.id = new_id
                by_id[new_id] = sym
                order.append(new_id)
        if remap:
            pf.contains = [(p, remap.get(c, c)) for p, c in pf.contains]
            pf.raw_calls = {remap.get(k, k): v for k, v in pf.raw_calls.items()}
            pf.class_methods = {
                t: {n: remap.get(i, i) for n, i in m.items()}
                for t, m in pf.class_methods.items()
            }
    return [by_id[i] for i in order], by_id


def _next_free(base: str, by_id: dict[str, Symbol]) -> str:
    n = 1
    while f"{base}#{n}" in by_id:
        n += 1
    return f"{base}#{n}"


def _merge_partial(keep: Symbol, other: Symbol) -> None:
    """`Default.aspx.cs` + `Default.aspx.designer.cs` are ONE class (§5.2)."""
    if other.path not in keep.path:
        keep.path = f"{keep.path};{other.path}"
    keep.source = f"{keep.source}\n\n// --- partial: {other.path} ---\n{other.source}"
    keep.end_line = max(keep.end_line, other.end_line)
    keep.is_webforms = keep.is_webforms or other.is_webforms
    keep.legacy_apis = sorted(set(keep.legacy_apis) | set(other.legacy_apis))
    keep.sql_procs = sorted(set(keep.sql_procs) | set(other.sql_procs))


def _build_reexports(parsed: list[ParsedFile], by_id: dict[str, Symbol]) -> dict[str, str]:
    """Map `pkg.name` -> the id where `name` is really defined.

    A package's `__init__.py` commonly re-exports:

        # pkg/__init__.py
        from .noise_schedule import cosine_schedule

    which makes `from pkg import cosine_schedule` legal. But the Symbol id is
    `pkg.noise_schedule.cosine_schedule`, so `pkg.cosine_schedule` matches
    nothing and every call through it is dropped. Measured on a real repo this
    was the second-largest group of unresolved calls.
    """
    out: dict[str, str] = {}
    for pf in parsed:
        head = pf.symbols[0]
        if not head.path.endswith("__init__.py"):
            continue
        for local_name, target in pf.import_map.items():
            if target in by_id:
                out[f"{head.id}.{local_name}"] = target
    return out


def _build_index(symbols: list[Symbol], by_id: dict[str, Symbol]) -> GlobalIndex:
    classes_by_name: dict[str, str] = {}
    methods_by_name: dict[str, list[str]] = defaultdict(list)
    file_ids: set[str] = set()
    for s in symbols:
        if s.kind in ("class", "interface"):
            classes_by_name.setdefault(s.name, s.id)
        elif s.kind == "method":
            methods_by_name[s.name].append(s.id)
        elif s.kind in ("file", "page"):
            file_ids.add(s.id)
    return GlobalIndex(
        by_id=by_id,
        qualified={s.id: s.id for s in symbols},
        file_ids=file_ids,
        classes_by_name=classes_by_name,
        methods_by_name=dict(methods_by_name),
    )


def parse_repo(
    repo_root: str, stats: dict | None = None
) -> tuple[list[Symbol], list[Edge]]:
    """Parse a repo into Symbols + edges.

    Pass a dict as `stats` to collect resolution diagnostics — chiefly how many
    call sites resolved versus were dropped. That ratio is the gate on whether
    the graph is trustworthy enough to guide refactoring: unresolved calls are
    dropped rather than guessed (CLAUDE.md §2.2), so a low rate means blast
    radius is quietly incomplete rather than wrong.
    """
    counters = stats if stats is not None else {}
    counters.setdefault("files_parsed", 0)
    counters.setdefault("files_failed", 0)
    counters.setdefault("calls_seen", 0)
    counters.setdefault("calls_resolved", 0)
    counters.setdefault("unresolved_names", Counter())
    counters.setdefault("failed_paths", [])

    parsed: list[ParsedFile] = []
    for path in iter_source_files(repo_root):
        try:
            pf = parse_file(path, repo_root)
            if pf is not None:
                parsed.append(pf)
                counters["files_parsed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad file must not kill indexing
            counters["files_failed"] += 1
            counters["failed_paths"].append(rel_path(path, repo_root))
            print(f"[parse] skipped {path}: {exc}")

    symbols, by_id = _collect_symbols(parsed)
    index = _build_index(symbols, by_id)
    index.reexports = _build_reexports(parsed, by_id)
    edges: list[Edge] = []

    # CONTAINS — recorded explicitly by each extractor where nesting is known
    for pf in parsed:
        for parent, child in pf.contains:
            if parent in by_id and child in by_id:
                edges.append(Edge(parent, child, "CONTAINS"))

    # IMPORTS — only when the imported target is itself an indexed file/page
    for pf in parsed:
        src_id = pf.symbols[0].id
        for mod in pf.imported_modules:
            if mod in index.file_ids and mod != src_id:
                edges.append(Edge(src_id, mod, "IMPORTS"))

    # INHERITS — import-aliased target first, then a repo-wide type name
    for pf in parsed:
        for type_id, bases in pf.raw_inherits.items():
            for base in bases:
                target = pf.import_map.get(base)
                if not (target and target in by_id):
                    target = index.classes_by_name.get(base)
                if target and target != type_id:
                    edges.append(Edge(type_id, target, "INHERITS"))

    # CALLS — per-language resolution, weighted by call-site count
    for pf in parsed:
        resolve = _resolver(pf)
        for caller_id, sites in pf.raw_calls.items():
            if caller_id not in by_id:
                continue
            counts: Counter[str] = Counter()
            for callee, is_self, receiver in sites:
                counters["calls_seen"] += 1
                target = resolve(caller_id, callee, is_self, receiver, pf, index)
                if target and target != caller_id and target in by_id:
                    counters["calls_resolved"] += 1
                    counts[target] += 1
                else:
                    counters["unresolved_names"][callee] += 1
            for target, n in counts.items():
                edges.append(Edge(caller_id, target, "CALLS", count=n))

    # HANDLES — markup-wired and lifecycle handlers (§5.1)
    for pf in parsed:
        if pf.language != aspx_ext.LANGUAGE:
            continue
        for page_id, method_id in aspx_ext.resolve_handlers(pf, index):
            if page_id in by_id and method_id in by_id:
                edges.append(Edge(page_id, method_id, "HANDLES"))

    # TRIGGERS — client-side __doPostBack -> the server handler that runs.
    # This closes the WebForms client/server gap: without it, changing a handler
    # never surfaces the JavaScript that fires it.
    handlers_by_page: dict[str, dict[str, str]] = {}
    for pf in parsed:
        for page_id, mapping in pf.control_handlers.items():
            handlers_by_page[page_id] = mapping
    # which pages pull in which script files
    pages_using: dict[str, list[str]] = defaultdict(list)
    for pf in parsed:
        src_id = pf.symbols[0].id
        for mod in pf.imported_modules:
            if mod in index.file_ids:
                pages_using[mod].append(src_id)

    for pf in parsed:
        for sym_id, targets in pf.postback_targets.items():
            owner_file = sym_id.split("::")[0]
            # an inline block belongs to its own page; a .js file to its users
            candidate_pages = ([owner_file] if owner_file in handlers_by_page
                               else pages_using.get(owner_file, []))
            for control_id in targets:
                for page_id in candidate_pages:
                    mapping = handlers_by_page.get(page_id, {})
                    handler = mapping.get(control_id)
                    if not handler:
                        continue
                    class_name = pf.codebehind.get(page_id) or _codebehind_of(
                        parsed, page_id
                    )
                    class_id = (class_name if class_name in by_id
                                else index.classes_by_name.get(
                                    (class_name or "").split(".")[-1]))
                    target_id = f"{class_id}.{handler}" if class_id else None
                    if target_id and target_id in by_id and sym_id in by_id:
                        edges.append(Edge(sym_id, target_id, "TRIGGERS"))

    # EXECUTES — method -> stored procedure, once procs are in the graph (§5.4)
    procs_by_name = {s.name.lower(): s.id for s in symbols if s.kind == "proc"}
    for sym in symbols:
        for proc in sym.sql_procs:
            target = procs_by_name.get(proc.lower())
            if target:
                edges.append(Edge(sym.id, target, "EXECUTES"))

    return symbols, edges


def _codebehind_of(parsed: list[ParsedFile], page_id: str) -> str:
    for pf in parsed:
        if page_id in pf.codebehind:
            return pf.codebehind[page_id]
    return ""


_RESOLVERS = {mod.LANGUAGE: mod.resolve_call for mod in _EXTRACTORS}


def _resolver(pf: ParsedFile):
    return _RESOLVERS.get(pf.language, python_ext.resolve_call)
