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


def list_communities() -> list[dict[str, Any]]:
    """Architectural overview: the Leiden communities and what each represents.
    Use this for 'global' / 'how is this organized' questions."""
    return graph._run(
        """
        MATCH (c:Community)
        RETURN c.id AS id, c.name AS name, c.summary AS summary,
               c.member_count AS member_count
        ORDER BY c.member_count DESC
        """
    )


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
            "community detection) with a one-line summary of each. Use this for broad/"
            "architectural questions like 'how is this codebase organized?'."
        ),
        "input_schema": {"type": "object", "properties": {}},
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
}


def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Execute a tool by name. Raises KeyError for unknown tools (the caller
    converts that into an is_error tool_result)."""
    fn = DISPATCH[name]
    return fn(**tool_input)
