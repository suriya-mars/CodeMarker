"""Scale profile: derive index/query parameters from the MEASURED size of a repo.

A single hardcoded configuration cannot serve both a 20k-LOC service and a 2M-LOC
monolith. Rather than ask the operator to tune constants, we measure the graph at
index time and derive everything from it.

Two categories, and the distinction matters:

  SCALES WITH THE CORPUS — bigger repo, different value
      Leiden resolution      : counters the modularity resolution limit (community.py)
      agent step budget      : longer paths need more hops
      summariser concurrency : serial is fine at 2k symbols, fatal at 200k
      graph render strategy  : you cannot draw 150,000 nodes
      incremental re-index   : optional when a full pass is 2 min, mandatory at 21 h

  DELIBERATELY FIXED — bounded by humans and context windows, not by repo size
      tool result limits     : the context window does not grow with the repo
      blast-radius refusal   : a 200-symbol change is unreviewable at ANY repo size
      summary length         : one sentence, always

Getting the second category wrong is the subtler failure: scaling tool limits up
with the repo floods the model exactly when precision matters most.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tier boundaries in symbol count. Chosen from what changes qualitatively:
#   tiny   — a full re-index is instant; render the whole graph
#   small  — a full re-index is a coffee break
#   medium — needs concurrency; render by community
#   large  — needs batching and incremental re-index to be usable at all
_TIERS = ((2_000, "tiny"), (20_000, "small"), (100_000, "medium"))


@dataclass
class ScaleProfile:
    symbols: int
    edges: int
    tier: str

    # --- scales with corpus ---
    max_agent_steps: int
    summary_workers: int
    use_batch_api: bool
    incremental_required: bool
    graph_render_limit: int
    render_communities_first: bool
    coarse_communities: int  # top level: must stay listable by an LLM
    fine_communities: int  # drill-down level

    # --- deliberately fixed (see module docstring) ---
    tool_result_limit: int = 15
    blast_radius_refuse_at: int = 200
    community_list_limit: int = 60

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    def explain(self) -> str:
        return (
            f"{self.symbols:,} symbols / {self.edges:,} edges -> tier '{self.tier}': "
            f"{self.summary_workers} summary workers"
            f"{' (batch API)' if self.use_batch_api else ''}, "
            f"{self.max_agent_steps} agent steps, "
            f"~{self.coarse_communities}/{self.fine_communities} communities, "
            f"incremental re-index {'REQUIRED' if self.incremental_required else 'optional'}"
        )


def derive(symbols: int, edges: int) -> ScaleProfile:
    """Build a profile from measured counts. Called once, after parsing."""
    tier = "large"
    for threshold, name in _TIERS:
        if symbols < threshold:
            tier = name
            break

    # Coarse level must stay listable — an LLM cannot reason over 3,000 clusters.
    # Grow sub-linearly and cap, so `list_communities` is useful at every size.
    coarse = max(4, min(40, int(symbols**0.4)))
    # Fine level targets reviewable clusters (~40 members).
    fine = max(coarse, symbols // 40)

    return ScaleProfile(
        symbols=symbols,
        edges=edges,
        tier=tier,
        max_agent_steps={"tiny": 6, "small": 8, "medium": 10, "large": 14}[tier],
        summary_workers={"tiny": 1, "small": 8, "medium": 16, "large": 16}[tier],
        use_batch_api=tier in ("medium", "large"),
        incremental_required=tier == "large",
        graph_render_limit={"tiny": 5_000, "small": 3_000, "medium": 1_500, "large": 1_000}[tier],
        render_communities_first=tier in ("medium", "large"),
        coarse_communities=coarse,
        fine_communities=fine,
    )
