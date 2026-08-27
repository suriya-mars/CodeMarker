"""Neo4j driver, the single `_run()` access point, and one-time schema setup.

All Cypher in the codebase goes through `_run()`. Anything derived from user
input MUST be passed as a parameter — never string-interpolated into Cypher.
The only interpolation we allow elsewhere is from a validated enum (see the
`kind` filter in tools.py).
"""

from __future__ import annotations

import os
from typing import Any

from neo4j import Driver, GraphDatabase

_driver: Driver | None = None


def get_driver() -> Driver:
    """Lazily create the singleton Bolt driver from the environment."""
    global _driver
    if _driver is None:
        uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        password = os.environ.get("NEO4J_PASSWORD", "change-me")
        _driver = GraphDatabase.driver(uri, auth=("neo4j", password))
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def _run(cypher: str, **params: Any) -> list[dict[str, Any]]:
    """Execute Cypher and return rows as plain dicts.

    Always parameterize user-derived values via **params.

    Parameters are passed as a dict, NOT as **kwargs: `Session.run`'s own first
    argument is named `query`, so a Cypher parameter called `$query` — which
    `find_symbol` always uses — would collide and raise
    "got multiple values for argument 'query'".
    """
    with get_driver().session() as session:
        result = session.run(cypher, parameters=params)
        return [record.data() for record in result]


# --- Schema -----------------------------------------------------------------

SCHEMA_STATEMENTS = [
    """
    CREATE CONSTRAINT symbol_id IF NOT EXISTS
      FOR (n:Symbol) REQUIRE n.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT community_id IF NOT EXISTS
      FOR (c:Community) REQUIRE c.id IS UNIQUE
    """,
    """
    CREATE FULLTEXT INDEX symbol_summaries IF NOT EXISTS
      FOR (n:Symbol) ON EACH [n.summary, n.name]
    """,
]


def setup_schema() -> None:
    """Create the uniqueness constraint and the full-text index (idempotent)."""
    for stmt in SCHEMA_STATEMENTS:
        _run(stmt)


def clear_graph() -> None:
    """Wipe all nodes/edges. Used before a full (non-incremental) re-index."""
    _run("MATCH (n) DETACH DELETE n")
