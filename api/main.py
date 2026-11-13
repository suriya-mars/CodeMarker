"""FastAPI surface: POST /index, POST /ask, GET /graph, /impact.

The frontend renders the graph from /graph and lights up the cited path returned
by /ask. The context package and the graph share one id space.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import agent
import auth
import graph
import guard
import impact
import modes
import pr_review
import quota
import repo_jobs
import sessions
import telemetry
import tools

auth.check_config()  # fail at boot on a bad AUTH_MODE, never per request
guard.check_config()  # likewise: never silently run without the ML guardrails
guard.preload()
pr_review.check_config()
try:  # a job the previous process was running cannot finish now
    repo_jobs.recover()
except Exception as exc:  # noqa: BLE001 — the database may not be up yet
    print(f"[repos] could not check for interrupted jobs: {exc}")
# Every route requires a user; auth.require_user exempts only /health.
app = FastAPI(title="RepoDigger", dependencies=[Depends(auth.require_user)])

# The web UI reaches the API same-origin (nginx proxies /api/ -> api:8000), so a
# browser needs NO cross-origin access. CORS is therefore off unless origins are
# listed explicitly — never "*", which would let any website a logged-in user
# visits call this API from their browser.
_cors = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if "*" in _cors:
    raise RuntimeError("CORS_ORIGINS must list exact origins; '*' is not allowed")
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

# Repos may only be indexed from directly under this root, and are named, not
# pathed: a caller-supplied path would let anyone walk (and then read back via
# get_definition) any directory the container can see.
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def repo_dir(name: str) -> Path:
    """Resolve a repo NAME to its directory, mounted (REPOS_ROOT) or cloned
    (CLONE_ROOT), or raise. Names, never paths: a caller-supplied path would let
    anyone walk, and then read back via get_definition, any directory the
    container can see. Errors never echo the resolved path."""
    if not _REPO_NAME.match(name or ""):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    path = repo_jobs.locate(name)
    if path is None:
        root = repo_jobs.repos_root()
        if (root / name).exists() or (repo_jobs.clone_root() / name).exists():
            raise HTTPException(status_code=400,
                                detail="repo must be a directory under the repos root")
        raise HTTPException(status_code=404, detail=f"no repo named {name!r}")
    return path


def _scrub(msg: str) -> str:
    return repo_jobs.scrub(msg)


# A question is resent on every agent turn, so its length multiplies into input
# cost. Pydantic rejects anything longer with a 422 before any work is done.
_MAX_QUESTION = int(os.environ.get("MAX_QUESTION_CHARS", "4000"))
# Every route that calls the LLM: requests per minute, then tokens per day.
_LLM_GUARDS = [Depends(auth.rate_limit), Depends(quota.guard)]


class IndexRequest(BaseModel):
    repo: str  # a directory name under REPOS_ROOT — not a path


class AddRepoRequest(BaseModel):
    url: str = Field(max_length=300)


class AskRequest(BaseModel):
    question: str = Field(max_length=_MAX_QUESTION)
    # Bind the agent's tools to one indexed repo. Server-side, so text injected
    # into the code cannot redirect the agent to read a different repo.
    repo: str = ""
    # A NAME, never a prompt: the system prompts live server-side (modes.py).
    mode: Literal["explore", "refactor", "security", "impact"] = "explore"
    # Remember this conversation: earlier turns are sent with the question. Without
    # a session_id a new conversation is started; its id comes back in the response.
    remember: bool = False
    session_id: str | None = Field(None, max_length=64)


class RefreshRequest(BaseModel):
    analyze: bool = False  # blast radius mode: analyse the change before re-indexing


NO_CHANGES = ("No new symbols or code to check blast radius. Select Blast radius mode, then "
              "refresh the repository after new commits arrive: the change is analysed before "
              "the graph is updated.")


class ImpactRequest(BaseModel):
    repo: str  # a directory name under REPOS_ROOT; also the graph scope
    base: str | None = None  # default: the commit the graph was indexed at
    head: str = "HEAD"
    depth: int = 3
    question: str | None = Field(None, max_length=_MAX_QUESTION)  # also get the LLM's analysis
    explain: bool = False  # LLM review summary with the default question


class ImpactAskRequest(BaseModel):
    question: str | None = Field(None, max_length=_MAX_QUESTION)


@app.on_event("shutdown")
def _shutdown() -> None:
    graph.close_driver()


@app.get("/usage")
def usage() -> dict[str, Any]:
    """Token, cost, and latency totals for this process — the answer to
    'what is this costing us?'. Counts only; no prompts are ever recorded."""
    return telemetry.snapshot()


@app.get("/me/usage")
def my_usage(request: Request, days: int = 30) -> dict[str, Any]:
    """The caller's OWN usage: tokens, estimated cost and questions per day.
    Never anyone else's — the all-users view is the operator CLI
    (`python -m quota report`) until the API has an admin role."""
    rows = quota.usage_report(days, auth.user_key(request))
    return {
        "user": auth.user_key(request),
        "days": rows,
        "total": {"questions": sum(r["questions"] for r in rows),
                  "tokens": sum(r["tokens"] for r in rows),
                  "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)},
        "daily_limit": quota.limit_for(request),
        "note": "cost is an estimate; the provider's console shows what is billed",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _queue(start, *args) -> None:
    try:
        start(*args)
    except repo_jobs.Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except repo_jobs.Exists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Indexing pays for a summary of every new or changed symbol, so these routes carry
# the same limits as the routes that answer questions.
@app.post("/index", status_code=202, dependencies=_LLM_GUARDS)
def index(req: IndexRequest, request: Request) -> dict[str, str]:
    """Re-index a mounted repo, as a background job (see GET /repos)."""
    repo_dir(req.repo)
    _queue(repo_jobs.refresh, req.repo, auth.user_key(request))
    return {"repo": req.repo, "status": "queued"}


@app.post("/repos", status_code=202, dependencies=_LLM_GUARDS)
def add_repo(req: AddRepoRequest, request: Request) -> dict[str, str]:
    """Clone a repository from its https URL, then index it, as a background job."""
    try:
        name = repo_jobs.add(req.url, auth.user_key(request))
    except (repo_jobs.Busy, repo_jobs.Exists) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo": name, "status": "queued"}


@app.post("/repos/{name}/refresh", status_code=202, dependencies=_LLM_GUARDS)
def refresh_repo(name: str, request: Request,
                 req: RefreshRequest | None = None) -> dict[str, str]:
    """Fetch the latest commit and re-index if there is one, as a background job.
    With `analyze`, the change is analysed for blast radius before re-indexing."""
    if not _REPO_NAME.match(name):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    analyze = bool(req and req.analyze)
    try:
        repo_jobs.refresh(name, auth.user_key(request), analyze=analyze)
    except repo_jobs.Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"repo": name, "status": "queued"}


@app.post("/ask", dependencies=_LLM_GUARDS)
def ask(req: AskRequest, request: Request) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    if req.repo and not _REPO_NAME.match(req.repo):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    user = auth.user_key(request)
    sid, history = None, None
    if req.remember:
        if not req.repo:
            raise HTTPException(status_code=400, detail="choose a repository to remember a conversation")
        if req.session_id:
            session = sessions.get(req.session_id, user)
            if session is None:
                raise HTTPException(status_code=404, detail="no such conversation")
            if session["repo"] != req.repo:
                raise HTTPException(status_code=400, detail="that conversation is about another repository")
            sid = req.session_id
            history = sessions.history(sid)
        else:
            sid = sessions.create(user, req.repo)
            history = []

    if req.mode == modes.IMPACT:
        if not req.repo:
            raise HTTPException(status_code=400, detail="choose a repository")
        report = repo_jobs.pending_impact(req.repo)
        if report is None:
            raise HTTPException(status_code=409, detail=NO_CHANGES)
        if not report.changed:
            raise HTTPException(status_code=409, detail=(
                "The latest refresh changed no code symbols, so there is no blast radius."))
        result = impact.ask(report, req.question, history=history)
    else:
        mode = modes.MODES[req.mode]
        # Step budget scales with the indexed repo (scale.py); missing profile is fine.
        prof = graph.get_profile()
        with tools.scoped(req.repo):
            result = agent.ask(req.question, max_steps=prof.get("max_agent_steps"),
                               system=mode.system, tool_names=mode.tool_names, history=history)
    quota.charge(request, result.get("usage"))
    with tools.scoped(req.repo):
        try:
            result["path"] = tools.connect(result.get("cited_ids") or [])
        except Exception as exc:  # noqa: BLE001 — a paid-for answer outlives a failed highlight
            print(f"[ask] could not connect cited symbols: {exc}")
            result["path"] = {"nodes": [], "edges": []}
    if sid:
        sessions.append(sid, req.question, result.get("answer") or "", req.mode,
                        result.get("cited_ids"), result.get("path"))
        result["session_id"] = sid
        result["history_turns"] = len(history or []) // 2
    return result


@app.get("/sessions")
def list_sessions(repo: str, request: Request) -> list[dict[str, Any]]:
    """The caller's own remembered conversations about one repo, newest first."""
    if not _REPO_NAME.match(repo):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    return sessions.list_for(auth.user_key(request), repo)


@app.get("/sessions/{sid}")
def get_session(sid: str, request: Request) -> dict[str, Any]:
    session = sessions.get(sid, auth.user_key(request))
    if session is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    turns = sessions.turns(sid)
    for t in turns:
        t["path"] = json.loads(t["path"] or "{}")
    return {"id": sid, "repo": session["repo"], "title": session.get("title", ""), "turns": turns}


@app.delete("/sessions/{sid}")
def delete_session(sid: str, request: Request) -> dict[str, str]:
    if not sessions.delete(sid, auth.user_key(request)):
        raise HTTPException(status_code=404, detail="no such conversation")
    return {"status": "deleted"}


@app.post("/impact", dependencies=[Depends(auth.rate_limit)])
def impact_report(req: ImpactRequest, request: Request) -> dict[str, Any]:
    """Impact of a commit range. The report is deterministic and free; the LLM
    runs only if a question is given (or `explain`), and reads the report's
    compact digest rather than the source. So only that case needs token quota,
    and a user who has spent theirs can still get the report."""
    path = repo_dir(req.repo)
    if req.question or req.explain:
        quota.guard(request)
    try:
        report = impact.analyze(str(path), base=req.base, head=req.head,
                                repo=req.repo, depth=req.depth)
    except (RuntimeError, ValueError) as exc:  # bad revision, not a repo, not indexed
        raise HTTPException(status_code=400, detail=_scrub(str(exc))) from exc
    out: dict[str, Any] = {"report": report.to_dict(),
                           "markdown": impact.to_markdown(report)}
    if req.question or req.explain:
        out["analysis"] = impact.ask(report, req.question)
        quota.charge(request, out["analysis"].get("usage"))
    return out


@app.get("/impact")
def impact_list(repo: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return impact.list_reports(repo, limit)


@app.get("/impact/range")
def impact_range(repo: str) -> dict[str, Any]:
    """What can be compared in this repo, for the web UI. No LLM call."""
    return impact.range_status(str(repo_dir(repo)), repo)


@app.get("/impact/{report_id}")
def impact_get(report_id: str) -> dict[str, Any]:
    report = impact.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no such report")
    return {"report": report.to_dict(), "markdown": impact.to_markdown(report)}


@app.post("/impact/{report_id}/ask", dependencies=_LLM_GUARDS)
def impact_ask(report_id: str, req: ImpactAskRequest, request: Request) -> dict[str, Any]:
    """Ask a follow-up about a stored report — no recomputation."""
    report = impact.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no such report")
    result = impact.ask(report, req.question)
    quota.charge(request, result.get("usage"))
    return result


@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict[str, str]:
    """GitHub pull request events. Answers fast, because GitHub gives up after 10
    seconds, and does the work on pr_review's queue.

    Not found unless a webhook secret is configured. Nothing in the body is read until
    the signature proves GitHub sent it; the size limit runs first because checking a
    signature needs the whole body in memory.
    """
    if not pr_review.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > pr_review.MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
    if not pr_review.verify_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        status = pr_review.handle_event(request.headers.get("x-github-event", ""), body)
    except pr_review.QueueFull as exc:
        raise HTTPException(status_code=503, detail="review queue is full") from exc
    return {"status": status}


# What "Open code" serves: classes and functions, never whole files.
_CODE_KINDS = ["class", "interface", "function", "method"]
_MAX_CODE_CHARS = 200_000  # a generated class must not stall the browser


@app.get("/symbol/source")
def symbol_source(id: str, repo: str = "") -> dict[str, Any]:
    """One symbol's stored source, for the web UI's code viewer. Read from the graph:
    no LLM call and no cost. The id is a query parameter because ids contain `/` and
    `::`, and is always passed to Cypher as a parameter."""
    if repo and not _REPO_NAME.match(repo):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    rows = graph._run(
        """
        MATCH (s:Symbol {id: $id})
        WHERE s.kind IN $kinds AND ($repo = '' OR s.repo = $repo)
        RETURN s.id AS id, s.name AS name, s.kind AS kind, s.path AS path,
               s.language AS language, s.start_line AS start_line,
               s.end_line AS end_line, s.source AS source
        """,
        id=id,
        kinds=_CODE_KINDS,
        repo=repo,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No class or function with that id")
    row = rows[0]
    source = row["source"] or ""
    row["truncated"] = len(source) > _MAX_CODE_CHARS
    row["source"] = source[:_MAX_CODE_CHARS]
    return row


@app.get("/repos")
def list_repos(request: Request) -> list[dict[str, Any]]:
    """Every repo with its index state: symbols, commit, and any job queued or
    running. The web UI polls this while a job is active."""
    return repo_jobs.list_repos(auth.user_key(request))


@app.get("/graph")
def get_graph(limit: int | None = None, repo: str = "") -> dict[str, list[dict[str, Any]]]:
    """Return the Symbol graph for visualization: nodes + edges in a Cytoscape-
    friendly shape, optionally for one repo. CONTAINS is included so a function
    sits next to its file instead of floating unconnected; the UI draws it faintly.

    The node cap comes from the repo's scale profile — you cannot draw 150,000
    nodes, so a large repo returns the highest-degree subgraph and the UI is
    expected to drill in by community from there. Edges are taken only among the
    returned nodes, with real dependencies ahead of CONTAINS if the edge cap bites."""
    if repo and not _REPO_NAME.match(repo):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    prof = graph.get_profile()
    if limit is None:
        limit = prof.get("graph_render_limit", 1500)
    limit = max(1, min(int(limit), 5000))
    nodes = graph._run(
        """
        MATCH (s:Symbol) WHERE $repo = '' OR s.repo = $repo
        OPTIONAL MATCH (s)-[c:CALLS]-()
        WITH s, count(c) AS degree
        RETURN s.id AS id, s.name AS name, s.kind AS kind, s.path AS path,
               s.summary AS summary, s.community_id AS community_id, degree
        ORDER BY degree DESC, id
        LIMIT $limit
        """,
        repo=repo,
        limit=limit,
    )
    edges = graph._run(
        """
        MATCH (a:Symbol)-[r:CALLS|IMPORTS|INHERITS|CONTAINS|HANDLES|TRIGGERS|EXECUTES]->(b:Symbol)
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS source, b.id AS target, type(r) AS type,
               coalesce(r.count, 1) AS count
        ORDER BY type = 'CONTAINS', source, target
        LIMIT $limit
        """,
        ids=[n["id"] for n in nodes],
        limit=limit * 5,
    )
    return {"nodes": nodes, "edges": edges}
