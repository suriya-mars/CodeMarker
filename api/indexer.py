"""Indexing pipeline orchestrator.

    repo files
      → tree-sitter parse (parsing.parse_repo)
      → write Symbols + edges to Neo4j
      → LLM summary per Symbol (summarize)
      → full-text index (created by graph.setup_schema)
      → Leiden communities (community) → community_id + Community nodes
      → LLM summary per Community

CLI:  python -m indexer /repos/<repo-name>
"""

from __future__ import annotations

import sys
from dataclasses import asdict

import community as community_mod
import graph
import summarize
from parsing import Edge, Symbol, parse_repo

# Relationship types we are allowed to write. Cypher rel-types can't be
# parameterized, so we dispatch per (validated) type — never interpolate raw input.
_EDGE_TYPES = {"CONTAINS", "IMPORTS", "CALLS", "INHERITS", "HANDLES", "EXECUTES"}
_BATCH = 500


def _batches(items, size=_BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def write_symbols(symbols: list[Symbol]) -> None:
    rows = [asdict(s) for s in symbols]
    for batch in _batches(rows): # one BATCH --> 500 rows[dict : symbols] to reduce overhead of Node creation
        graph._run(
            """
            UNWIND $rows AS row
            MERGE (s:Symbol {id: row.id})
            SET s += row
            """,
            rows=batch,
        )


def write_edges(edges: list[Edge]) -> None:
    for edge_type in _EDGE_TYPES:
        rows = [
            {"src": e.src, "dst": e.dst, "count": e.count}
            for e in edges
            if e.type == edge_type
        ]
        if not rows:
            continue
        # edge_type is from a validated enum — safe to interpolate.
        cypher = f"""
            UNWIND $rows AS row
            MATCH (a:Symbol {{id: row.src}})
            MATCH (b:Symbol {{id: row.dst}})
            MERGE (a)-[r:{edge_type}]->(b)
            SET r.count = row.count
        """
        for batch in _batches(rows):
            graph._run(cypher, rows=batch)


def write_summaries(symbols: list[Symbol]) -> None:
    rows = []
    for s in symbols:
        s.summary = summarize.summarize_symbol(s)
        rows.append({"id": s.id, "summary": s.summary})
    for batch in _batches(rows):
        graph._run(
            """
            UNWIND $rows AS row
            MATCH (s:Symbol {id: row.id})
            SET s.summary = row.summary
            """,
            rows=batch,
        )


def write_communities(
    assignments: dict[str, str],
    communities: dict[str, list[str]],
    summaries_by_symbol: dict[str, str],
) -> None:
    if not communities:
        return
    # set community_id on each member symbol
    rows = [{"id": sid, "cid": cid} for sid, cid in assignments.items()]
    for batch in _batches(rows):
        graph._run(
            """
            UNWIND $rows AS row
            MATCH (s:Symbol {id: row.id})
            SET s.community_id = row.cid
            """,
            rows=batch,
        )

    community_rows = []
    for cid, members in communities.items():
        member_summaries = [summaries_by_symbol.get(m, m) for m in members]
        name = f"Community {cid}"
        summary = summarize.summarize_community(name, member_summaries)
        community_rows.append(
            {"id": cid, "name": name, "summary": summary, "member_count": len(members)}
        )

    graph._run(
        """
        UNWIND $rows AS row
        MERGE (c:Community {id: row.id})
        SET c.name = row.name, c.summary = row.summary, c.member_count = row.member_count
        """,
        rows=community_rows,
    )
    # IN_COMMUNITY edges
    for batch in _batches(rows):
        graph._run(
            """
            UNWIND $rows AS row
            MATCH (s:Symbol {id: row.id})
            MATCH (c:Community {id: row.cid})
            MERGE (s)-[:IN_COMMUNITY]->(c)
            """,
            rows=batch,
        )


def index_repo(repo_path: str) -> dict[str, int]:
    print("[index] schema setup")
    graph.setup_schema()
    graph.clear_graph()  # MVP: full re-index

    print(f"[index] parsing {repo_path}")
    symbols, edges = parse_repo(repo_path)
    print(f"[index] {len(symbols)} symbols, {len(edges)} edges")

    write_symbols(symbols)
    write_edges(edges)

    print("[index] summarizing symbols (LLM)")
    write_summaries(symbols)
    summaries_by_symbol = {s.id: s.summary for s in symbols}

    print("[index] Leiden community detection")
    assignments, communities = community_mod.detect_communities(symbols, edges)
    print(f"[index] {len(communities)} communities")
    write_communities(assignments, communities, summaries_by_symbol)

    print("[index] done")
    return {
        "symbols": len(symbols),
        "edges": len(edges),
        "communities": len(communities),
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m indexer <repo-path>")
        raise SystemExit(2)
    stats = index_repo(sys.argv[1])
    print(stats)
    graph.close_driver()


if __name__ == "__main__":
    main()
