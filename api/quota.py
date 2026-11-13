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

Privacy: usage records carry the user's email, so they are kept for
USAGE_RETENTION_DAYS (default 400, about 13 months) and then deleted, once a day.
To remove everything stored about one person (a leaver, or a deletion request):

    python -m quota forget alice@example.com          # shows what would go
    python -m quota forget alice@example.com --yes    # deletes it
    python -m quota prune                             # apply retention now
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


def retention_days() -> int:
    return int(os.environ.get("USAGE_RETENTION_DAYS", "400"))


def prune(days: int | None = None) -> int:
    """Delete usage records older than the retention period. Returns how many."""
    days = retention_days() if days is None else days
    if days <= 0:
        return 0  # 0 = keep forever
    cutoff = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=days)).isoformat()
    rows = graph._run(
        "MATCH (q:UsageQuota) WHERE q.day < $cutoff DELETE q RETURN count(*) AS n",
        cutoff=cutoff,
    )
    return rows[0]["n"] if rows else 0


_pruned_on: str | None = None


def _prune_daily() -> None:
    """Retention without a scheduler: the first charge of each UTC day prunes."""
    global _pruned_on
    today = dt.datetime.now(dt.UTC).date().isoformat()
    if _pruned_on != today:
        _pruned_on = today
        try:
            prune()
        except Exception as exc:  # noqa: BLE001 — retention must never fail a request
            print(f"[quota] retention prune failed: {exc}")


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
    _prune_daily()


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


def forget(who: str, *, dry_run: bool = True) -> dict:
    """Everything stored about one person, found by login subject or email, and
    deleted unless `dry_run`. Returns what was (or would be) removed.

    Deleted: usage records, quota overrides, remembered conversations. Repositories
    they added are shared with everyone, so they stay, with the person's name removed.
    Not covered here: the Cognito account, and backups and snapshots, which expire on
    their own schedule (see the CLI output)."""
    who = who.strip()
    key = who.lower() if "@" in who else who
    # A person is known by a login subject (Cognito's opaque id) AND an email; usage
    # records hold both, so either one finds the other.
    rows = graph._run(
        """
        MATCH (q:UsageQuota) WHERE q.user = $key OR q.email = $key
        RETURN collect(DISTINCT q.user) AS subs, collect(DISTINCT q.email) AS emails
        """,
        key=key,
    )
    subs = {key} | set(rows[0]["subs"] if rows else [])
    emails = ({key} if "@" in key else set()) | {e for e in (rows[0]["emails"] if rows else []) if e}
    ids = sorted(subs | emails)
    found = graph._run(
        """
        CALL () { MATCH (q:UsageQuota) WHERE q.user IN $ids OR q.email IN $ids
                  RETURN count(q) AS usage }
        CALL () { MATCH (u:UserQuota) WHERE u.user IN $ids RETURN count(u) AS overrides }
        CALL () { MATCH (s:Session) WHERE s.user IN $ids
                  OPTIONAL MATCH (s)-[:HAS_TURN]->(t:Turn)
                  RETURN count(DISTINCT s) AS conversations, count(t) AS turns }
        CALL () { MATCH (r:Repo) WHERE r.added_by IN $ids OR r.job_user IN $ids
                  RETURN count(r) AS repos }
        RETURN usage, overrides, conversations, turns, repos
        """,
        ids=ids,
    )[0]
    result = {"identities": ids, **found, "deleted": not dry_run}
    if dry_run:
        return result
    graph._run("MATCH (q:UsageQuota) WHERE q.user IN $ids OR q.email IN $ids DELETE q", ids=ids)
    graph._run("MATCH (u:UserQuota) WHERE u.user IN $ids DELETE u", ids=ids)
    graph._run(
        """
        MATCH (s:Session) WHERE s.user IN $ids
        OPTIONAL MATCH (s)-[:HAS_TURN]->(t:Turn)
        DETACH DELETE s, t
        """,
        ids=ids,
    )
    graph._run(
        """
        MATCH (r:Repo) WHERE r.added_by IN $ids OR r.job_user IN $ids
        SET r.added_by = CASE WHEN r.added_by IN $ids THEN 'a removed user' ELSE r.added_by END,
            r.job_user = CASE WHEN r.job_user IN $ids THEN 'a removed user' ELSE r.job_user END
        """,
        ids=ids,
    )
    return result


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
    s_forget = sub.add_parser("forget", help="delete everything stored about one person")
    s_forget.add_argument("user", help="login subject or email")
    s_forget.add_argument("--yes", action="store_true", help="really delete (default: only show)")
    s_prune = sub.add_parser("prune", help="delete usage records past retention now")
    s_prune.add_argument("--days", type=int, default=None,
                         help="override USAGE_RETENTION_DAYS for this run")
    args = ap.parse_args()
    if args.cmd == "set" and args.daily_tokens < 0:
        ap.error("daily_tokens must be >= 0 (0 = unlimited)")

    graph.setup_schema()
    if args.cmd == "forget":
        r = forget(args.user, dry_run=not args.yes)
        verb = "Deleted" if r["deleted"] else "Would delete"
        print(f"{verb} for {', '.join(r['identities'])}:")
        print(f"  {r['usage']} usage record(s), {r['overrides']} quota override(s), "
              f"{r['conversations']} conversation(s) with {r['turns']} turn(s)")
        print(f"  and remove their name from {r['repos']} repository record(s); "
              f"the repositories stay")
        if not r["deleted"]:
            print("Nothing changed. Run again with --yes to delete.")
        print("Not covered here:\n"
              "  - the login: aws cognito-idp admin-delete-user --user-pool-id <pool> --username <email>\n"
              "  - backups and snapshots, which still hold this data until they expire\n"
              "    (S3 dumps after the bucket's retention, EBS snapshots after their count)")
        graph.close_driver()
        return
    if args.cmd == "prune":
        n = prune(args.days)
        print(f"deleted {n} usage record(s) older than "
              f"{args.days if args.days is not None else retention_days()} day(s)")
        graph.close_driver()
        return
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
