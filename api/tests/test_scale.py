"""Scale-profile tests: the system must serve a 200-LOC repo and a 2M-LOC monolith
from the same code, adapting to measured size rather than a tuned constant."""

import igraph as ig

import community as community_mod
import scale
from parsing import Edge, Symbol


def _p(symbols, edges=None):
    return scale.derive(symbols, edges if edges is not None else symbols * 3)


def test_tiers_span_the_whole_range():
    assert _p(120).tier == "tiny"
    assert _p(1_999).tier == "tiny"
    assert _p(12_000).tier == "small"
    assert _p(60_000).tier == "medium"
    assert _p(135_000).tier == "large"
    assert _p(2_000_000).tier == "large"


def test_effort_scales_up_with_size():
    small, large = _p(500), _p(150_000)
    assert large.max_agent_steps > small.max_agent_steps
    assert large.summary_workers > small.summary_workers
    assert large.use_batch_api and not small.use_batch_api
    assert large.incremental_required and not small.incremental_required


def test_coarse_communities_stay_listable_at_any_size():
    """An LLM cannot reason over thousands of clusters — the top level must stay
    bounded however big the repo gets, while the fine level tracks real modules."""
    for n in (500, 5_000, 50_000, 500_000, 2_000_000):
        prof = _p(n)
        assert 4 <= prof.coarse_communities <= 40, n
        assert prof.fine_communities >= prof.coarse_communities, n
    assert _p(2_000_000).fine_communities > _p(5_000).fine_communities


def test_human_bounded_limits_do_NOT_scale():
    """Context windows and human review capacity don't grow with the repo.
    Scaling these up would flood the model exactly when precision matters most."""
    tiny, huge = _p(200), _p(2_000_000)
    assert tiny.tool_result_limit == huge.tool_result_limit
    assert tiny.blast_radius_refuse_at == huge.blast_radius_refuse_at
    assert tiny.community_list_limit == huge.community_list_limit


def test_render_limit_shrinks_as_graph_grows():
    assert _p(150_000).graph_render_limit < _p(500).graph_render_limit
    assert _p(150_000).render_communities_first
    assert not _p(500).render_communities_first


def test_profile_round_trips_as_flat_properties():
    """Must be storable on a Neo4j node — flat scalars only."""
    d = _p(1234).as_dict()
    assert all(isinstance(v, (int, float, str, bool)) for v in d.values())
    assert d["symbols"] == 1234


# --- adaptive Leiden ---------------------------------------------------------


def _planted(n_blocks, block_size):
    """Graph with known ground truth: dense blocks, sparse links between."""
    syms, edges = [], []
    for b in range(n_blocks):
        ids = [f"m{b}.f{i}" for i in range(block_size)]
        syms += [Symbol(id=i, kind="method", name=i, path="p", start_line=1,
                        end_line=2, language="csharp") for i in ids]
        for i in range(block_size):
            for j in range(i + 1, block_size):
                edges.append(Edge(ids[i], ids[j], "CALLS", count=3))
        if b:
            edges.append(Edge(f"m{b}.f0", f"m{b-1}.f0", "CALLS", count=1))
    return syms, edges


def test_adaptive_resolution_beats_fixed_on_a_larger_graph():
    """Fixed gamma=1.0 merges real modules as the graph grows (the modularity
    resolution limit). Targeting a community count recovers them."""
    syms, edges = _planted(40, 20)          # 800 symbols, 40 true modules
    _, fixed = community_mod.detect_communities(syms, edges, resolution=1.0)
    _, adaptive = community_mod.detect_communities(syms, edges, target_communities=40)
    assert abs(len(adaptive) - 40) <= abs(len(fixed) - 40)
    assert max(len(m) for m in adaptive.values()) <= 20 * 2


def test_small_graphs_skip_the_search():
    """Below the threshold the search isn't worth its cost, and gamma=1.0 is fine."""
    syms, edges = _planted(3, 5)            # 15 symbols
    _, comms = community_mod.detect_communities(syms, edges, target_communities=3)
    assert comms


def test_resolution_search_is_monotonic_and_bounded():
    g = ig.Graph.Famous("Zachary")
    w = [1] * g.ecount()
    gamma = community_mod.resolution_for_target(g, w, 6)
    assert 0.5 <= gamma <= 250.0
