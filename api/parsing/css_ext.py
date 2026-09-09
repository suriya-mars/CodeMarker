"""CSS extractor.

Deliberately shallow: stylesheets create no call or dependency edges, so a full
symbol-per-rule model would inflate the graph for no navigational value. What is
useful is one node per stylesheet carrying the selectors it defines, so a page's
`CssClass="btn btn-primary"` can be traced to the file that styles it — and so a
migration can see which stylesheets a page actually depends on.

Vendored and minified stylesheets are skipped: bootstrap.min.css contains
thousands of selectors that tell you nothing about YOUR code. Its version, which
does matter, is captured by `aspx_ext` from the link tag.
"""

from __future__ import annotations

import os

from tree_sitter_languages import get_parser

from .base import (
    GlobalIndex,
    ParsedFile,
    Symbol,
    node_text,
    read_bytes,
    rel_path,
)

LANGUAGE = "css"
EXTENSIONS = (".css",)


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    src = read_bytes(file_path)
    root = get_parser(LANGUAGE).parse(src).root_node
    path = rel_path(file_path, repo_root)

    selectors = _collect_selectors(root, src)
    sym = Symbol(
        id=path,
        kind="file",
        name=os.path.basename(file_path),
        path=path,
        start_line=1,
        end_line=root.end_point[0] + 1,
        language=LANGUAGE,
        signature=f"{len(selectors)} selectors",
        selectors=selectors[:400],  # cap: a huge sheet must not bloat the node
    )
    return ParsedFile(symbols=[sym], language=LANGUAGE)


def _collect_selectors(root, src: bytes) -> list[str]:
    """Class and id selectors this sheet defines.

    Only names reached through a `class_selector` / `id_selector` parent count —
    the grammar also emits `class_name` for pseudo-classes, so `a:hover` would
    otherwise be recorded as a class named `hover`.
    """
    found: set[str] = set()

    def walk(n):
        if n.type in ("class_selector", "id_selector"):
            prefix = "." if n.type == "class_selector" else "#"
            for c in n.named_children:
                if c.type in ("class_name", "id_name"):
                    found.add(prefix + node_text(c, src))
        for c in n.named_children:
            walk(c)

    walk(root)
    return sorted(found)


def resolve_call(caller_id, callee_name, is_self, receiver, pf: ParsedFile,
                 index: GlobalIndex):
    """Stylesheets declare no calls."""
    return None
