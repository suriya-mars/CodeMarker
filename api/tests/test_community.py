import os

import community as community_mod
import parsing

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_repo")


def test_detect_communities_covers_call_graph():
    symbols, edges = parsing.parse_repo(FIXTURE)
    assignments, communities = community_mod.detect_communities(symbols, edges)

    call_nodes = {e.src for e in edges if e.type == "CALLS"} | {
        e.dst for e in edges if e.type == "CALLS"
    }
    # every node that participates in the CALLS graph gets a community
    assert set(assignments) == call_nodes
    assert communities
    # community membership is consistent with the assignment map
    for cid, members in communities.items():
        for m in members:
            assert assignments[m] == cid
