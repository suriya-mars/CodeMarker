"""Daily LLM token quota per user.

The rate limiter (auth.rate_limit) bounds how many requests a user makes; it
does not bound what they cost — twenty cheap questions and twenty that each
burn the full agent token budget count the same. This caps TOKENS per user per
UTC day.

Counters live in Neo4j, not in memory, so a restart does not reset anyone's
allowance and several api processes share one count. The increment is a single
Cypher statement, so concurrent requests never lose an update.

Checked before a request and charged after it: one request can overshoot the
quota by at most its own budget (AGENT_TOKEN_BUDGET), never more.
"""

from __future__ import annotations

import datetime as dt
import os

from fastapi import HTTPException, Request

import auth
import graph


def daily_limit() -> int:
    return int(os.environ.get("DAILY_TOKEN_QUOTA", "2000000"))


def _key(user: str) -> str:
    return f"{dt.datetime.now(dt.UTC).date().isoformat()}|{user}"


def used(user: str) -> int:
    rows = graph._run("MATCH (q:UsageQuota {key: $key}) RETURN q.tokens AS n", key=_key(user))
    return rows[0]["n"] if rows else 0


def guard(request: Request) -> None:
    """FastAPI dependency: refuse LLM work once today's quota is spent."""
    limit = daily_limit()
    if limit <= 0:
        return
    if used(auth.user_key(request)) >= limit:
        raise HTTPException(
            status_code=429,
            detail="daily token quota exhausted; it resets at 00:00 UTC",
        )


def charge(request: Request, usage: dict[str, int] | None) -> None:
    tokens = int((usage or {}).get("input_tokens", 0)) + int((usage or {}).get("output_tokens", 0))
    if tokens <= 0 or daily_limit() <= 0:
        return
    graph._run(
        """
        MERGE (q:UsageQuota {key: $key})
        ON CREATE SET q.user = $user, q.day = $day, q.tokens = 0
        SET q.tokens = q.tokens + $tokens
        """,
        key=_key(auth.user_key(request)),
        user=auth.user_key(request),
        day=dt.datetime.now(dt.UTC).date().isoformat(),
        tokens=tokens,
    )
