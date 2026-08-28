"""Graph tools the LLM uses to navigate the codebase, plus their Cypher.

The LLM is the retrieval policy; these tools are the only graph operations it
gets. Keep them few and capable. All user-derived values are passed as Cypher
parameters; the only interpolation is from a validated enum (the `kind` filter).

To add a tool: add its schema to TOOLS, write the function + Cypher, register it
in DISPATCH.
"""

from __future__ import annotations

import re
from typing import Any

import graph

_KINDS = {"file", "class", "interface", "function", "method", "page", "proc", "table"}
# What we return for a Symbol in tool results — never the full source unless asked.
_SYMBOL_FIELDS = (
    "s.id AS id, s.kind AS kind, s.name AS name, s.path AS path, "
    "s.summary AS summary, s.signature AS signature, s.language AS language, "
    "s.community_id AS community_id"
)


def _clean_lucene(query: str) -> str:
    """Strip Lucene operators so a plain-English query won't throw a parse error."""
    cleaned = re.sub(r'[+\-!(){}\[\]^"~*?:\\/]', " ", query)
    return cleaned.strip() or query


# --- Tool implementations ---------------------------------------------------


def find_symbol(query: str, kind: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Lexical full-text search over Symbol summaries + names (the vectorless
    entry point). Searches summaries, not raw code."""
    kind_filter = ""
    params: dict[str, Any] = {"query": _clean_lucene(query), "limit": int(limit)}
    if kind in _KINDS:  # validated enum — safe to interpolate
        kind_filter = f"WHERE node.kind = '{kind}'"
    cypher = f"""
        CALL db.index.fulltext.queryNodes('symbol_summaries', $query)
        YIELD node, score
        WITH node AS s, score
        {kind_filter}
        RETURN {_SYMBOL_FIELDS}, score
        ORDER BY score DESC
        LIMIT $limit
    """
    return graph._run(cypher, **params)


def list_communities(limit: int = 40) -> list[dict[str, Any]]:
    """Architectural overview: the Leiden communities and what each represents.
    Use this for 'global' / 'how is this organized' questions.

    Bounded: a 2M-LOC repo yields thousands of communities, and returning them all
    would flood the context window. Largest-first, so the truncation keeps the
    architecturally significant ones."""
    limit = max(1, min(int(limit), 100))
    rows = graph._run(
        """
        MATCH (c:Community)
        RETURN c.id AS id, c.name AS name, c.summary AS summary,
               c.member_count AS member_count
        ORDER BY c.member_count DESC
        LIMIT $limit
        """,
        limit=limit,
    )
    total = graph._run("MATCH (c:Community) RETURN count(c) AS n")
    n = total[0]["n"] if total else len(rows)
    if n > len(rows):
        rows.append({"note": f"{n - len(rows)} smaller communities not shown; "
                             f"use find_symbol to reach them directly"})
    return rows


def get_community(community_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """The member Symbols of one community."""
    return graph._run(
        f"""
        MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community {{id: $community_id}})
        RETURN {_SYMBOL_FIELDS}
        ORDER BY s.kind, s.name
        LIMIT $limit
        """,
        community_id=community_id,
        limit=int(limit),
    )


def get_callers(symbol_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """Functions/methods that CALL the given symbol (who depends on it)."""
    return graph._run(
        f"""
        MATCH (s:Symbol)-[r:CALLS]->(target:Symbol {{id: $symbol_id}})
        RETURN {_SYMBOL_FIELDS}, r.count AS call_count
        ORDER BY call_count DESC
        LIMIT $limit
        """,
        symbol_id=symbol_id,
        limit=int(limit),
    )


def get_callees(symbol_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """Functions/methods the given symbol CALLS (what it depends on)."""
    return graph._run(
        f"""
        MATCH (source:Symbol {{id: $symbol_id}})-[r:CALLS]->(s:Symbol)
        RETURN {_SYMBOL_FIELDS}, r.count AS call_count
        ORDER BY call_count DESC
        LIMIT $limit
        """,
        symbol_id=symbol_id,
        limit=int(limit),
    )


def get_definition(symbol_id: str) -> dict[str, Any]:
    """Full detail for one symbol: source, location, and its containment context
    (parent + children via CONTAINS, inheritance, imports)."""
    rows = graph._run(
        """
        MATCH (s:Symbol {id: $symbol_id})
        OPTIONAL MATCH (parent:Symbol)-[:CONTAINS]->(s)
        OPTIONAL MATCH (s)-[:CONTAINS]->(child:Symbol)
        OPTIONAL MATCH (s)-[:INHERITS]->(base:Symbol)
        RETURN s.id AS id, s.kind AS kind, s.name AS name, s.path AS path,
               s.signature AS signature, s.summary AS summary,
               s.start_line AS start_line, s.end_line AS end_line,
               s.source AS source,
               parent.id AS parent,
               collect(DISTINCT child.id) AS children,
               collect(DISTINCT base.id) AS inherits
        """,
        symbol_id=symbol_id,
    )
    if not rows:
        return {"error": f"no symbol with id '{symbol_id}'"}
    row = rows[0]
    # trim very long source bodies in tool context
    if row.get("source") and len(row["source"]) > 4000:
        row["source"] = row["source"][:4000] + "\n# ...(truncated)"
    return row


def get_blast_radius(symbol_id: str, depth: int = 3, limit: int = 40) -> list[dict[str, Any]]:
    """Everything that transitively REACHES this symbol — what breaks if you
    change it. Follows CALLS and HANDLES upstream, so WebForms pages show up via
    their event handlers rather than being invisible."""
    depth = max(1, min(int(depth), 5))  # validated int — safe to interpolate
    return graph._run(
        f"""
        MATCH (s:Symbol)-[:CALLS|HANDLES*1..{depth}]->(:Symbol {{id: $symbol_id}})
        WITH DISTINCT s
        RETURN {_SYMBOL_FIELDS}, s.is_webforms AS is_webforms
        ORDER BY s.kind, s.id
        LIMIT $limit
        """,
        symbol_id=symbol_id,
        limit=int(limit),
    )


def get_migration_blockers(symbol_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Legacy WebForms APIs (Session/ViewState/HttpContext/…) that have no .NET
    Core equivalent. With `symbol_id`, reports blockers inside that subtree; with
    no argument, ranks the whole app's pages by how blocked they are."""
    if symbol_id:
        return graph._run(
            """
            MATCH (:Symbol {id: $symbol_id})-[:CONTAINS|HANDLES|CALLS*0..3]->(m:Symbol)
            WHERE m.legacy_apis IS NOT NULL AND size(m.legacy_apis) > 0
            RETURN DISTINCT m.id AS id, m.name AS name, m.path AS path,
                   m.legacy_apis AS legacy_apis
            ORDER BY size(m.legacy_apis) DESC, m.id
            LIMIT $limit
            """,
            symbol_id=symbol_id,
            limit=int(limit),
        )
    return graph._run(
        """
        MATCH (p:Symbol {kind: 'page'})-[:HANDLES|CONTAINS*1..3]->(m:Symbol)
        WHERE m.legacy_apis IS NOT NULL AND size(m.legacy_apis) > 0
        UNWIND m.legacy_apis AS api
        WITH p, collect(DISTINCT m.id) AS methods, collect(DISTINCT api) AS apis
        RETURN p.id AS id, p.name AS name, p.path AS path,
               size(methods) AS blocker_methods, apis AS legacy_apis
        ORDER BY size(apis) DESC, blocker_methods DESC
        LIMIT $limit
        """,
        limit=int(limit),
    )


def get_page_brief(page_id: str, depth: int = 3) -> dict[str, Any]:
    """Everything needed to migrate ONE WebForms page, in a single call.

    Built for page-by-page migration: instead of five round trips
    (get_definition + get_callees + get_migration_blockers + get_blast_radius +
    proc lookup) this assembles the whole briefing server-side, which is both
    cheaper in tokens and matches how the work is actually done.

    Accepts either the .aspx page id or its code-behind class id.
    """
    depth = max(1, min(int(depth), 5))  # validated int — safe to interpolate

    rows = graph._run(
        """
        MATCH (n:Symbol {id: $id})
        OPTIONAL MATCH (p:Symbol {kind: 'page'})-[:HANDLES]->(n)
        RETURN n.id AS id, n.kind AS kind, n.name AS name, n.path AS path,
               n.summary AS summary, n.is_webforms AS is_webforms,
               collect(DISTINCT p.id)[0] AS owning_page
        """,
        id=page_id,
    )
    if not rows:
        return {"error": f"no symbol with id '{page_id}'"}
    node = rows[0]
    # normalise: if given a code-behind class, brief its page instead
    root = node["owning_page"] or node["id"]

    entry = graph._run(
        """
        MATCH (p:Symbol {id: $id})-[:HANDLES]->(h:Symbol)
        WHERE h.kind = 'method'
        RETURN h.id AS id, h.name AS name, h.signature AS signature,
               h.summary AS summary, h.legacy_apis AS legacy_apis
        ORDER BY h.name
        """,
        id=root,
    )

    _REACH_FIELDS = (
        "d.id AS id, d.name AS name, d.path AS path, d.kind AS kind, "
        "d.summary AS summary, d.legacy_apis AS legacy_apis, d.sql_procs AS sql_procs"
    )
    reach = graph._run(
        f"""
        MATCH (p:Symbol {{id: $id}})-[:HANDLES]->(h:Symbol)
        WHERE h.kind = 'method'
        MATCH (h)-[:CALLS*0..{depth}]->(d:Symbol)
        WITH DISTINCT d
        RETURN {_REACH_FIELDS}
        ORDER BY d.id
        LIMIT 60
        """,
        id=root,
    )

    # Interface indirection: `_svc.Save()` resolves to IOrderService.Save, which
    # has no body and no procs. The real implementation — and the stored
    # procedures it executes — is only reachable backwards through INHERITS.
    # Without this hop, a DI-heavy codebase hides all its data access.
    impls = graph._run(
        f"""
        MATCH (p:Symbol {{id: $id}})-[:HANDLES]->(h:Symbol)
        WHERE h.kind = 'method'
        MATCH (h)-[:CALLS*0..{depth}]->(m:Symbol)
        MATCH (iface:Symbol)-[:CONTAINS]->(m)
        MATCH (impl_type:Symbol)-[:INHERITS]->(iface)
        MATCH (impl_type)-[:CONTAINS]->(d:Symbol)
        WHERE d.name = m.name AND d.id <> m.id
        WITH DISTINCT d
        RETURN {_REACH_FIELDS}
        ORDER BY d.id
        LIMIT 40
        """,
        id=root,
    )
    seen_reach = {r["id"] for r in reach}
    for r in impls:
        if r["id"] not in seen_reach:
            r = dict(r)
            r["via"] = "implementation of an interface reached from this page"
            reach.append(r)
            seen_reach.add(r["id"])

    dependents = graph._run(
        f"""
        MATCH (p:Symbol {{id: $id}})-[:HANDLES]->(own:Symbol)
        MATCH (caller:Symbol)-[:CALLS*1..{depth}]->(own)
        WHERE NOT (p)-[:HANDLES]->(caller)
        WITH DISTINCT caller
        RETURN caller.id AS id, caller.path AS path, caller.kind AS kind
        ORDER BY caller.id
        LIMIT 40
        """,
        id=root,
    )

    controls = graph._run(
        "MATCH (p:Symbol {id: $id})-[:IMPORTS]->(c:Symbol) RETURN c.id AS id",
        id=root,
    )

    # Count per DISTINCT symbol — handlers appear in both lists (CALLS*0.. includes
    # the handler itself), so a naive sum double-counts every blocker.
    blockers: dict[str, int] = {}
    procs: set[str] = set()
    counted: set[str] = set()
    for r in entry + reach:
        if r["id"] in counted:
            continue
        counted.add(r["id"])
        for api in r.get("legacy_apis") or []:
            blockers[api] = blockers.get(api, 0) + 1
        for proc in r.get("sql_procs") or []:
            procs.add(proc)

    return {
        "page": root,
        "path": node["path"],
        "summary": node["summary"],
        "is_webforms": node["is_webforms"],
        "handlers": entry,
        "reachable_code": reach,
        "stored_procedures": sorted(procs),
        "migration_blockers": dict(sorted(blockers.items(), key=lambda kv: -kv[1])),
        "depended_on_by": dependents,
        "user_controls": [c["id"] for c in controls],
        "effort_hint": _effort_hint(len(reach), len(blockers), len(dependents)),
    }


def _effort_hint(reach: int, blockers: int, dependents: int) -> str:
    """Deterministic first-pass sizing, so the model does not have to guess."""
    score = reach + blockers * 3 + dependents * 2
    if score < 15:
        return "small — self-contained, few dependents"
    if score < 50:
        return "medium — review the listed blockers and callers before starting"
    return "large — consider splitting; many dependents or heavy legacy API use"


# --- Tool schemas (Anthropic tool-use format) -------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_symbol",
        "description": (
            "Search the codebase by meaning using lexical full-text search over "
            "LLM-generated one-line summaries and names. This is the primary entry "
            "point for specific questions ('how does login work?'). Returns matching "
            "symbols (files/classes/functions/methods) with their ids and summaries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "kind": {
                    "type": "string",
                    "enum": [
                        "file", "class", "interface", "function", "method",
                        "page", "proc", "table",
                    ],
                    "description": (
                        "Optional filter by symbol kind. 'page' is an .aspx/.ascx "
                        "WebForms page; 'proc' is a SQL Server stored procedure."
                    ),
                },
                "limit": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_communities",
        "description": (
            "List the architectural communities (clusters of related code from Leiden "
            "community detection) with a one-line summary of each, largest first. Use "
            "this for broad/architectural questions like 'how is this codebase "
            "organized?'. On a large repo the list is truncated — it tells you how "
            "many were omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max communities (default 40)."}},
        },
    },
    {
        "name": "get_community",
        "description": "List the member symbols of one community, given its id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "community_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["community_id"],
        },
    },
    {
        "name": "get_callers",
        "description": (
            "Given a symbol id, return the functions/methods that CALL it — trace a "
            "flow upstream (who triggers this)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "get_callees",
        "description": (
            "Given a symbol id, return the functions/methods it CALLS — trace a flow "
            "downstream (what it does next)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "get_page_brief",
        "description": (
            "THE migration entry point for a single WebForms page. Given an .aspx "
            "page id (or its code-behind class id), returns everything needed to "
            "migrate it in one call: event handlers, all code reachable from them, "
            "stored procedures touched, legacy-API blockers with counts, what else "
            "depends on this page's code, and a deterministic effort hint. Prefer "
            "this over calling get_definition/get_callees/get_migration_blockers "
            "separately when working page by page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": ".aspx page id or code-behind class id."},
                "depth": {"type": "integer", "description": "Call hops to follow, 1-5 (default 3)."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "get_blast_radius",
        "description": (
            "Given a symbol id, return everything that transitively DEPENDS ON it — "
            "the impact of changing it. Follows CALLS and HANDLES upstream, so "
            "WebForms pages appear via their event handlers. Use for 'what breaks "
            "if I change X' and for migration sequencing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "depth": {"type": "integer", "description": "Hops to follow, 1-5 (default 3)."},
                "limit": {"type": "integer"},
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "get_migration_blockers",
        "description": (
            "Report legacy ASP.NET WebForms API usage (Session, ViewState, "
            "HttpContext, IsPostBack, …) that has no direct .NET Core equivalent. "
            "Pass a symbol_id to scope it to one page/class, or omit it to rank the "
            "whole application's pages by how hard they are to migrate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Optional. Omit for an app-wide ranking.",
                },
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_definition",
        "description": (
            "Given a symbol id, return its full source code, location, and containment "
            "context (parent, children, inheritance). Use to read the actual code once "
            "you've located a symbol."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol_id": {"type": "string"}},
            "required": ["symbol_id"],
        },
    },
]

DISPATCH = {
    "find_symbol": find_symbol,
    "list_communities": list_communities,
    "get_community": get_community,
    "get_callers": get_callers,
    "get_callees": get_callees,
    "get_definition": get_definition,
    "get_blast_radius": get_blast_radius,
    "get_migration_blockers": get_migration_blockers,
    "get_page_brief": get_page_brief,
}


def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Execute a tool by name. Raises KeyError for unknown tools (the caller
    converts that into an is_error tool_result)."""
    fn = DISPATCH[name]
    return fn(**tool_input)
