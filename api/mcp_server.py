"""MCP server: the read-only graph surface, for Claude Code.

Deployment model (PRODUCTION-DOTNET.md §7): each engineer runs this locally over
stdio; it holds a Bolt connection to ONE shared Neo4j. That split matters —

  * the shared thing is the DATABASE, not an HTTP API, so there is no public
    endpoint to secure and no `/index` reachable by teammates
  * one index means everyone gets the same blast-radius answer, which is the
    point when the codebase is being actively refactored
  * this process exposes ONLY the read tools; indexing is a separate job run by
    one owner or by CI on merge

Every tool is read-only, enforced two ways: `_assert_read_only()` refuses to start
if a writing tool is ever registered, and each tool carries `read_only_hint` so
the client knows it too. The model has no write capability here at all — Claude
Code's own Edit/Write tools do the editing, under your review.

Run:
    NEO4J_URI=bolt://graphhost:7687 NEO4J_PASSWORD=... python -m mcp_server
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import graph
import tools

_WRITE_CYPHER = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DETACH|DROP|LOAD CSV)\b")
_SCHEMAS = {t["name"]: t for t in tools.TOOLS}


def _assert_read_only() -> None:
    """Refuse to start if anything writable is exposed.

    A capability guarantee, not a policy one: there is no write path to police,
    so no prompt can produce one. If someone later adds a writing tool to
    DISPATCH, this server stops booting rather than quietly gaining the ability.
    """
    offenders = []
    for name, fn in tools.DISPATCH.items():
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            # Cannot read it -> cannot verify it. Fail CLOSED: an unverifiable
            # tool is treated as unsafe, never waved through.
            offenders.append(f"{name} (source unavailable — cannot verify)")
            continue
        found = sorted(set(_WRITE_CYPHER.findall(src)))
        if found:
            offenders.append(f"{name} ({', '.join(found)})")
    if offenders:
        raise RuntimeError(
            "refusing to start: MCP exposes only read tools, but these can write: "
            + "; ".join(offenders)
        )


def _describe(name: str) -> str:
    return _SCHEMAS.get(name, {}).get("description", name)


def build_server() -> MCPServer:
    _assert_read_only()

    server = MCPServer(
        name="codebase-explorer",
        version="0.1.0",
        instructions=(
            "Read-only graph tools over an indexed codebase (Python, C#, "
            "ASP.NET WebForms) plus its SQL Server dependencies.\n\n"
            "Start with get_page_brief for a WebForms page you are migrating — it "
            "returns handlers, reachable code, stored procedures, legacy-API "
            "blockers and dependents in one call. Use find_symbol to locate "
            "anything by meaning, get_blast_radius before changing shared code, "
            "and list_communities for an architectural overview.\n\n"
            "These tools cannot modify the codebase. Propose changes; the human "
            "applies them."
        ),
    )

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                               idempotent_hint=True, open_world_hint=False)
    for name, fn in sorted(tools.DISPATCH.items()):
        server.add_tool(fn, name=name, description=_describe(name),
                        annotations=read_only)
    return server


def preflight() -> dict[str, Any]:
    """Verify the graph is reachable and indexed before serving.

    Failing loudly here beats every tool call returning an empty list, which
    looks like 'this code does not exist' rather than 'the database is empty'.
    """
    info: dict[str, Any] = {"uri": os.environ.get("NEO4J_URI", "bolt://neo4j:7687")}
    try:
        rows = graph._run("MATCH (s:Symbol) RETURN count(s) AS n")
        info["symbols"] = rows[0]["n"] if rows else 0
        info["profile"] = graph.get_profile() or None
        info["ok"] = info["symbols"] > 0
        if not info["ok"]:
            info["hint"] = "graph is empty — run: python -m indexer <path>"
    except Exception as exc:  # noqa: BLE001
        info |= {"ok": False, "error": str(exc),
                 "hint": "check NEO4J_URI / NEO4J_PASSWORD and that Neo4j is reachable"}
    return info


def main() -> None:
    status = preflight()
    # stdout is the MCP transport — diagnostics MUST go to stderr or the
    # protocol stream is corrupted and the client fails to connect.
    print(json.dumps({"event": "mcp.preflight", **status}), file=sys.stderr, flush=True)
    if not status.get("ok"):
        print(f"[mcp] WARNING: {status.get('hint', 'graph unavailable')}",
              file=sys.stderr, flush=True)
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
