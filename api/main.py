"""FastAPI surface: POST /index, POST /ask, GET /graph, /impact.

The frontend renders the graph from /graph and lights up the cited path returned
by /ask. The context package and the graph share one id space.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import agent
import auth
import graph
import impact
import indexer
import quota
import telemetry
import tools

auth.check_config()  # fail at boot on a bad AUTH_MODE, never per request
# Every route requires a user; auth.require_user exempts only /health.
app = FastAPI(title="Codebase Explorer", dependencies=[Depends(auth.require_user)])

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
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

# Repos may only be indexed from directly under this root, and are named, not
# pathed: a caller-supplied path would let anyone walk (and then read back via
# get_definition) any directory the container can see.
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _repos_root() -> Path:
    return Path(os.environ.get("REPOS_ROOT", "/repos")).resolve()


def repo_dir(name: str) -> Path:
    """Resolve a repo NAME to its directory under REPOS_ROOT, or raise.

    The regex rejects separators and `..`; resolving and re-checking the parent
    also rejects a symlink inside the root that points outside it. Errors never
    echo the resolved path.
    """
    if not _REPO_NAME.match(name or ""):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    root = _repos_root()
    path = (root / name).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="repo must be a directory under the repos root")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"no repo named {name!r}")
    return path


def _scrub(msg: str) -> str:
    return msg.replace(str(_repos_root()), "<repos>")


# A question is resent on every agent turn, so its length multiplies into input
# cost. Pydantic rejects anything longer with a 422 before any work is done.
_MAX_QUESTION = int(os.environ.get("MAX_QUESTION_CHARS", "4000"))
# Every route that calls the LLM: requests per minute, then tokens per day.
_LLM_GUARDS = [Depends(auth.rate_limit), Depends(quota.guard)]


class IndexRequest(BaseModel):
    repo: str  # a directory name under REPOS_ROOT — not a path


class AskRequest(BaseModel):
    question: str = Field(max_length=_MAX_QUESTION)
    # Bind the agent's tools to one indexed repo. Server-side, so text injected
    # into the code cannot redirect the agent to read a different repo.
    repo: str = ""


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/index")
def index(req: IndexRequest) -> dict[str, Any]:
    path = repo_dir(req.repo)
    try:
        return indexer.index_repo(str(path), repo=req.repo)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc


@app.post("/ask", dependencies=_LLM_GUARDS)
def ask(req: AskRequest, request: Request) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    if req.repo and not _REPO_NAME.match(req.repo):
        raise HTTPException(status_code=400, detail="repo must be a plain directory name")
    # Step budget scales with the indexed repo (scale.py); missing profile is fine.
    prof = graph.get_profile()
    with tools.scoped(req.repo):
        result = agent.ask(req.question, max_steps=prof.get("max_agent_steps"))
    quota.charge(request, result.get("usage"))
    return result


@app.post("/impact", dependencies=_LLM_GUARDS)
def impact_report(req: ImpactRequest, request: Request) -> dict[str, Any]:
    """Impact of a commit range. The report is deterministic and free; the LLM
    runs only if a question is given (or `explain`), and reads the report's
    compact digest rather than the source."""
    path = repo_dir(req.repo)
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


@app.get("/graph")
def get_graph(limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return the Symbol graph for visualization: nodes + edges in a Cytoscape-
    friendly shape. CALLS/IMPORTS/INHERITS edges only (CONTAINS is implied by the
    id hierarchy and would clutter the overview).

    The node cap comes from the repo's scale profile — you cannot draw 150,000
    nodes, so a large repo returns the highest-degree subgraph and the UI is
    expected to drill in by community from there."""
    prof = graph.get_profile()
    if limit is None:
        limit = prof.get("graph_render_limit", 1500)
    nodes = graph._run(
        """
        MATCH (s:Symbol)
        OPTIONAL MATCH (s)-[c:CALLS]-()
        WITH s, count(c) AS degree
        RETURN s.id AS id, s.name AS name, s.kind AS kind, s.path AS path,
               s.summary AS summary, s.community_id AS community_id, degree
        ORDER BY degree DESC
        LIMIT $limit
        """,
        limit=int(limit),
    )
    edges = graph._run(
        """
        MATCH (a:Symbol)-[r:CALLS|IMPORTS|INHERITS]->(b:Symbol)
        RETURN a.id AS source, b.id AS target, type(r) AS type,
               coalesce(r.count, 1) AS count
        LIMIT $limit
        """,
        limit=int(limit) * 3,
    )
    return {"nodes": nodes, "edges": edges}
