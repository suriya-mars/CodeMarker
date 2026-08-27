"""Leiden community detection over the CALLS graph.

Runs in Python (`leidenalg` + `python-igraph`), NOT Neo4j GDS — GDS Leiden
availability is edition-dependent and we avoid that dependency (CLAUDE.md §2.4).

CALLS edges are weighted by call count so hub functions (`utils.log()` etc.)
don't smear communities across the graph.
"""

from __future__ import annotations

from parsing import Edge, Symbol

import igraph as ig
import leidenalg as la


def detect_communities(
    symbols: list[Symbol],
    edges: list[Edge],
    *,
    resolution: float = 1.0,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Partition the CALLS graph into communities.

    Returns:
      assignments  : symbol_id -> community_id (only for nodes in the CALLS graph)
      communities  : community_id -> [member symbol ids]
    """
    call_edges = [e for e in edges if e.type == "CALLS"]
    if not call_edges:
        return {}, {}

    # vertex set = symbols that participate in at least one CALLS edge
    node_ids = sorted({e.src for e in call_edges} | {e.dst for e in call_edges})
    index = {nid: i for i, nid in enumerate(node_ids)}

    g = ig.Graph(directed=False)
    g.add_vertices(len(node_ids))
    g.add_edges([(index[e.src], index[e.dst]) for e in call_edges])
    weights = [max(1, e.count) for e in call_edges]

    partition = la.find_partition(
        g,
        la.RBConfigurationVertexPartition,
        weights=weights,
        resolution_parameter=resolution,
        seed=42,
    )

    assignments: dict[str, str] = {}
    communities: dict[str, list[str]] = {}
    for member_indices in partition:
        if not member_indices:
            continue
        cid = f"c{len(communities)}"
        members = [node_ids[i] for i in member_indices]
        communities[cid] = members
        for m in members:
            assignments[m] = cid

    return assignments, communities
    #assignments: symbol_id → community_id (e.g. "auth.AuthService.login" → "c0")
    #communities: community_id → [member ids] (e.g. "c0" → ["...login", "..._check", ...])