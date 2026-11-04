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

Per-user overrides are `(:UserQuota {user, daily_tokens})` nodes, matched on the
login subject OR email (with Cognito the subject is an opaque UUID; the email is
what an admin knows). They are set by an operator from the CLI below — never over
HTTP, since the API has no admin role and a route would let any user raise their
own limit:

    python -m quota set alice@example.com 5000000   # tokens per UTC day
    python -m quota set bob@example.com 0           # unlimited
    python -m quota clear alice@example.com         # back to DAILY_TOKEN_QUOTA
    python -m quota list                            # overrides + today's usage
"""

from __future__ import annotations

import datetime as dt
import os

from fastapi import HTTPException, Request

import auth
import graph


def daily_limit() -> int:
    """The default for users with no override."""
    return int(os.environ.get("DAILY_TOKEN_QUOTA", "2000000"))


def _identities(request: Request) -> list[str]:
    user = getattr(request.state, "user", None) or {}
    ids = [auth.user_key(request)]
    if user.get("email"):
        ids.append(str(user["email"]).lower())
    return ids


def limit_for(request: Request) -> int:
    """This user's daily limit: their override if one exists, else the default.
    An override of 0 means unlimited, exactly like DAILY_TOKEN_QUOTA=0."""
    ids = _identities(request)
    rows = graph._run(  # an override on the login subject beats one on the email
        """
        MATCH (u:UserQuota) WHERE u.user IN $ids
        RETURN u.daily_tokens AS n ORDER BY (u.user = $ids[0]) DESC LIMIT 1
        """,
        ids=ids,
    )
    return int(rows[0]["n"]) if rows else daily_limit()


def _key(user: str) -> str:
    return f"{dt.datetime.now(dt.UTC).date().isoformat()}|{user}"


def used(user: str) -> int:
    rows = graph._run("MATCH (q:UsageQuota {key: $key}) RETURN q.tokens AS n", key=_key(user))
    return rows[0]["n"] if rows else 0


def guard(request: Request) -> None:
    """FastAPI dependency: refuse LLM work once today's quota is spent."""
    limit = limit_for(request)
    if limit <= 0:
        return
    if used(auth.user_key(request)) >= limit:
        raise HTTPException(
            status_code=429,
            detail="daily token quota exhausted; it resets at 00:00 UTC",
        )


def charge(request: Request, usage: dict | None) -> None:
    """Record one request's usage against the caller, for today (UTC).

    One node per user per day carries tokens (in/out), estimated cost and the
    number of questions — the quota reads `tokens`; the usage report reads all
    of it. Charged even for unlimited users, so reports show real spend."""
    usage = usage or {}
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    if tokens_in + tokens_out <= 0:
        return
    ids = _identities(request)
    graph._run(
        """
        MERGE (q:UsageQuota {key: $key})
        ON CREATE SET q.user = $user, q.day = $day, q.tokens = 0, q.input_tokens = 0,
                      q.output_tokens = 0, q.cost_usd = 0.0, q.questions = 0
        SET q.tokens = q.tokens + $tokens,
            q.input_tokens = coalesce(q.input_tokens, 0) + $tokens_in,
            q.output_tokens = coalesce(q.output_tokens, 0) + $tokens_out,
            q.cost_usd = coalesce(q.cost_usd, 0.0) + $cost,
            q.questions = coalesce(q.questions, 0) + 1,
            q.email = $email
        """,
        key=_key(ids[0]),
        user=ids[0],
        email=ids[1] if len(ids) > 1 else None,
        day=dt.datetime.now(dt.UTC).date().isoformat(),
        tokens=tokens_in + tokens_out,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=float(usage.get("cost_usd", 0.0)),
    )


def usage_report(days: int = 30, user: str | None = None) -> list[dict]:
    """Per user per day: questions, tokens, estimated cost. `user` matches the
    login subject or the email."""
    days = max(1, min(int(days), 366))
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days - 1)).isoformat()
    who = user.lower() if user and "@" in user else user
    return graph._run(
        """
        MATCH (q:UsageQuota) WHERE q.day >= $since
          AND ($who IS NULL OR q.user = $who OR q.email = $who)
        RETURN q.day AS day, q.user AS user, q.email AS email,
               coalesce(q.questions, 0) AS questions,
               coalesce(q.input_tokens, 0) AS input_tokens,
               coalesce(q.output_tokens, 0) AS output_tokens,
               q.tokens AS tokens, round(coalesce(q.cost_usd, 0.0), 4) AS cost_usd
        ORDER BY day DESC, cost_usd DESC
        """,
        since=since,
        who=who,
    )


def totals(rows: list[dict]) -> list[dict]:
    """Collapse report rows to one line per user, most expensive first."""
    by: dict[str, dict] = {}
    for r in rows:
        t = by.setdefault(r["user"], {"user": r["user"], "email": r["email"],
                                      "questions": 0, "tokens": 0, "cost_usd": 0.0})
        t["questions"] += r["questions"]
        t["tokens"] += r["tokens"]
        t["cost_usd"] = round(t["cost_usd"] + r["cost_usd"], 4)
        t["email"] = t["email"] or r["email"]
    return sorted(by.values(), key=lambda t: -t["cost_usd"])


# --- operator CLI --------------------------------------------------------------


def set_override(user: str, daily_tokens: int) -> None:
    if daily_tokens < 0:
        raise ValueError("daily_tokens must be >= 0 (0 = unlimited)")
    graph._run(
        "MERGE (u:UserQuota {user: $user}) SET u.daily_tokens = $n, u.updated_at = $t",
        user=user.lower() if "@" in user else user, n=int(daily_tokens),
        t=dt.datetime.now(dt.UTC).isoformat(),
    )


def clear_override(user: str) -> int:
    rows = graph._run(
        "MATCH (u:UserQuota {user: $user}) DELETE u RETURN count(*) AS n",
        user=user.lower() if "@" in user else user,
    )
    return rows[0]["n"] if rows else 0


def overview() -> list[dict]:
    today = dt.datetime.now(dt.UTC).date().isoformat()
    return graph._run(
        """
        CALL () {
          MATCH (u:UserQuota) RETURN u.user AS user, u.daily_tokens AS cap
          UNION
          MATCH (q:UsageQuota {day: $today}) RETURN q.user AS user, null AS cap
        }
        WITH user, max(cap) AS cap
        OPTIONAL MATCH (q:UsageQuota {day: $today})
          WHERE q.user = user OR q.email = user
        RETURN user, cap, coalesce(sum(q.tokens), 0) AS used_today
        ORDER BY used_today DESC, user
        """,
        today=today,
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m quota",
                                 description="Per-user daily LLM token quotas.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s_set = sub.add_parser("set", help="set a user's daily token limit (0 = unlimited)")
    s_set.add_argument("user", help="login subject or email")
    s_set.add_argument("daily_tokens", type=int)
    s_clear = sub.add_parser("clear", help="remove an override; the default applies again")
    s_clear.add_argument("user")
    sub.add_parser("list", help="overrides and today's usage")
    s_rep = sub.add_parser("report", help="tokens and cost per user")
    s_rep.add_argument("--days", type=int, default=30)
    s_rep.add_argument("--user", help="one user's daily breakdown (subject or email)")
    args = ap.parse_args()
    if args.cmd == "set" and args.daily_tokens < 0:
        ap.error("daily_tokens must be >= 0 (0 = unlimited)")

    graph.setup_schema()
    if args.cmd == "set":
        set_override(args.user, args.daily_tokens)
        print(f"{args.user}: {args.daily_tokens or 'unlimited'} tokens/day")
    elif args.cmd == "clear":
        n = clear_override(args.user)
        print(f"{args.user}: override removed" if n else f"{args.user}: no override")
    elif args.cmd == "report":
        rows = usage_report(args.days, args.user)
        print(f"last {args.days} day(s) · cost is an ESTIMATE from telemetry.py prices; "
              f"the provider's console is what you are billed")
        if args.user:
            print(f"  {'day':<11} {'questions':>9} {'input':>11} {'output':>10} {'cost $':>10}")
            for r in rows:
                print(f"  {r['day']:<11} {r['questions']:>9} {r['input_tokens']:>11,} "
                      f"{r['output_tokens']:>10,} {r['cost_usd']:>10.4f}")
        else:
            print(f"  {'user':<40} {'questions':>9} {'tokens':>12} {'cost $':>10}")
            for t in totals(rows):
                name = t["email"] or t["user"]
                print(f"  {name:<40} {t['questions']:>9} {t['tokens']:>12,} {t['cost_usd']:>10.4f}")
        all_cost = sum(r["cost_usd"] for r in rows)
        print(f"  total: {sum(r['questions'] for r in rows)} questions, "
              f"{sum(r['tokens'] for r in rows):,} tokens, ${all_cost:.4f}")
    else:
        default = daily_limit()
        print(f"default: {default or 'unlimited'} tokens/day")
        for r in overview():
            lim = r["cap"]
            shown = "default" if lim is None else ("unlimited" if lim == 0 else f"{lim:,}")
            print(f"  {r['user']:<40} limit {shown:<12} used today {r['used_today']:,}")
    graph.close_driver()


if __name__ == "__main__":
    main()
