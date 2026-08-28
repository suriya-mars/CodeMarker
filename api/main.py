"""FastAPI surface: POST /index, POST /ask, GET /graph.

The frontend renders the graph from /graph and lights up the cited path returned
by /ask. The context package and the graph share one id space.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent
import graph
import indexer
import telemetry

app = FastAPI(title="Codebase Explorer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only tool; the web container talks to this API
    allow_methods=["*"],
    allow_headers=["*"],
)


class IndexRequest(BaseModel):
    path: str


class AskRequest(BaseModel):
    question: str


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
    try:
        return indexer.index_repo(req.path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    # Step budget scales with the indexed repo (scale.py); missing profile is fine.
    prof = graph.get_profile()
    return agent.ask(req.question, max_steps=prof.get("max_agent_steps"))


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
