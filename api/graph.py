"""Neo4j driver, the single `_run()` access point, and one-time schema setup.

All Cypher in the codebase goes through `_run()`. Anything derived from user
input MUST be passed as a parameter — never string-interpolated into Cypher.
The only interpolation we allow elsewhere is from a validated enum (see the
`kind` filter in tools.py).
"""

from __future__ import annotations

import os
from typing import Any

from neo4j import Driver, GraphDatabase, NotificationDisabledClassification

_driver: Driver | None = None


def get_driver() -> Driver:
    """Lazily create the singleton Bolt driver from the environment."""
    global _driver
    if _driver is None:
        uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        password = os.environ.get("NEO4J_PASSWORD", "")
        # No default: a fallback password is a password every deployment shares.
        if not password or password == "change-me":
            raise RuntimeError(
                "NEO4J_PASSWORD is unset or still the 'change-me' placeholder; "
                "set a real password in .env"
            )
        # UNRECOGNIZED = "relationship type does not exist". Traversals name every
        # edge type (HANDLES, EXECUTES, ...) and a Python-only repo has none of
        # the WebForms ones, so without this every blast-radius query logs a
        # warning for a type that is simply, and correctly, absent.
        _driver = GraphDatabase.driver(
            uri,
            auth=("neo4j", password),
            notifications_disabled_classifications=[
                NotificationDisabledClassification.UNRECOGNIZED
            ],
        )
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
    CREATE CONSTRAINT community_summary_fingerprint IF NOT EXISTS
      FOR (c:CommunitySummary) REQUIRE c.fingerprint IS UNIQUE
    """,
    """
    CREATE CONSTRAINT summary_cache_key IF NOT EXISTS
      FOR (c:SummaryCache) REQUIRE c.key IS UNIQUE
    """,
    """
    CREATE CONSTRAINT session_id IF NOT EXISTS
      FOR (s:Session) REQUIRE s.id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT usage_quota_key IF NOT EXISTS
      FOR (q:UsageQuota) REQUIRE q.key IS UNIQUE
    """,
    """
    CREATE CONSTRAINT user_quota_user IF NOT EXISTS
      FOR (u:UserQuota) REQUIRE u.user IS UNIQUE
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


def get_profile() -> dict[str, Any]:
    """The scale profile written at index time, or {} before a first index.

    Query-time code adapts to the indexed repo's measured size rather than
    assuming one. Never raises — callers fall back to their own defaults.
    """
    try:
        rows = _run("MATCH (m:RepoMeta {id: 'current'}) RETURN m {.*} AS p")
        return rows[0]["p"] if rows else {}
    except Exception:  # noqa: BLE001 — a missing profile must never break a query
        return {}


def clear_code_graph(repo: str) -> int:
    """Delete ONE repo's code graph: its symbols with their edges, and its indexed
    commit. Used by a full (`--fresh`) re-index. Returns the symbols deleted.

    Everything else stays: other repos, user data (usage, quotas, conversations,
    repo records, impact reports) and the summary caches. Communities are recomputed
    by the community stage. Deleted in batches, so a large repo does not need one
    huge transaction."""
    if not repo:
        raise ValueError("clear_code_graph needs a repo name")
    n = _run("MATCH (s:Symbol {repo: $repo}) RETURN count(s) AS n", repo=repo)[0]["n"]
    _run(
        """
        MATCH (s:Symbol {repo: $repo})
        CALL (s) { DETACH DELETE s } IN TRANSACTIONS OF 5000 ROWS
        """,
        repo=repo,
    )
    _run("MATCH (r:RepoState {repo: $repo}) DELETE r", repo=repo)
    return n


def wipe_test_database() -> None:
    """Delete EVERYTHING. Tests only: it refuses unless this process is connected to
    the test database, so it can never empty a real one by mistake."""
    test_uri = os.environ.get("NEO4J_TEST_URI")
    if not test_uri or os.environ.get("NEO4J_URI") != test_uri:
        raise RuntimeError("refusing to wipe: NEO4J_URI is not the test database (NEO4J_TEST_URI)")
    _run("MATCH (n) DETACH DELETE n")
