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
    return agent.ask(req.question)


@app.get("/graph")
def get_graph(limit: int = 1500) -> dict[str, list[dict[str, Any]]]:
    """Return the Symbol graph for visualization: nodes + edges in a Cytoscape-
    friendly shape. CALLS/IMPORTS/INHERITS edges only (CONTAINS is implied by the
    id hierarchy and would clutter the overview)."""
    nodes = graph._run(
        """
        MATCH (s:Symbol)
        OPTIONAL MATCH (s)-[c:CALLS]-()
        RETURN s.id AS id, s.name AS name, s.kind AS kind, s.path AS path,
               s.summary AS summary, s.community_id AS community_id,
               count(c) AS degree
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
