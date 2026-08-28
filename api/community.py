"""Leiden community detection over the CALLS graph.

Runs in Python (`leidenalg` + `python-igraph`), NOT Neo4j GDS — GDS Leiden
availability is edition-dependent and we avoid that dependency (CLAUDE.md §2.4).

CALLS edges are weighted by call count so hub functions (`utils.log()` etc.)
don't smear communities across the graph.

**Resolution scales with graph size.** Modularity has a known resolution limit: it
cannot resolve communities smaller than roughly sqrt(2m), so as a repo grows, real
modules get absorbed into mega-clusters at a fixed gamma. Measured on a synthetic
graph with known ground truth:

    15,000 symbols, 200 true modules
      gamma = 1.0    -> 174 communities, largest 150   (modules merged)
      adaptive       -> 200 communities, largest  75   (exact recovery)

    50,000 symbols, 500 true modules
      gamma = 1.0    -> 218 communities, largest 400   (4 modules merged into one)
      adaptive       -> 500 communities, largest 100   (exact recovery)

So a fixed `resolution=1.0` is only correct for small repos. Pass
`target_communities` (from `scale.ScaleProfile`) and the right gamma is searched
for at index time.
"""

from __future__ import annotations

from parsing import Edge, Symbol

import igraph as ig
import leidenalg as la


def _partition(g, weights, resolution: float):
    return la.find_partition(
        g,
        la.RBConfigurationVertexPartition,
        weights=weights,
        resolution_parameter=resolution,
        seed=42,
    )


def resolution_for_target(g, weights, target: int, *, iters: int = 8) -> float:
    """Binary-search the resolution that yields ~`target` communities.

    Community count rises monotonically with gamma, so bisection converges. Six
    iterations is enough in practice and bounds the cost — each one is a full
    Leiden run, which is seconds on a real call graph but not free.
    """
    # Upper bound must clear what large graphs actually need: a 50k-node graph
    # with 500 planted modules required gamma ~= 50 for exact recovery, so 60
    # would be a ceiling at 150k+. Widening costs nothing — iterations are fixed.
    lo, hi = 0.5, 250.0
    best_gamma, best_err = 1.0, None
    for _ in range(iters):
        mid = (lo + hi) / 2
        found = sum(1 for c in _partition(g, weights, mid) if c)
        err = abs(found - target)
        if best_err is None or err < best_err:
            best_gamma, best_err = mid, err
        if found < target:
            lo = mid
        else:
            hi = mid
    return best_gamma


def detect_communities(
    symbols: list[Symbol],
    edges: list[Edge],
    *,
    resolution: float = 1.0,
    target_communities: int | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Partition the CALLS graph into communities.

    `target_communities` (from `scale.derive(...)`) makes the resolution adapt to
    graph size instead of using a fixed gamma that only suits small repos.

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

    # Searching is only worth its cost once the resolution limit can actually bite.
    if target_communities and len(node_ids) >= 500:
        resolution = resolution_for_target(g, weights, target_communities)

    partition = _partition(g, weights, resolution)

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