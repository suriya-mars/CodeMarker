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

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

import community as community_mod
import graph
import scale
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


def pending_summaries(limit: int | None = None) -> list[Symbol]:
    """Symbols still missing a summary, read back from the graph.

    Reading the work queue from Neo4j rather than from the in-memory symbol list
    is what makes summarisation **resumable**: a crashed 21-hour run resumes
    exactly where it stopped, and re-running is idempotent. Everything already
    summarised is simply not returned.
    """
    rows = graph._run(
        f"""
        MATCH (s:Symbol)
        WHERE s.summary IS NULL OR s.summary = ''
        RETURN s.id AS id, s.kind AS kind, s.name AS name, s.path AS path,
               s.language AS language, s.signature AS signature,
               s.source AS source, s.start_line AS start_line, s.end_line AS end_line
        {f'LIMIT {int(limit)}' if limit else ''}
        """
    )
    return [
        Symbol(
            id=r["id"], kind=r["kind"] or "", name=r["name"] or "",
            path=r["path"] or "", start_line=r["start_line"] or 1,
            end_line=r["end_line"] or 1, language=r["language"] or "",
            signature=r["signature"] or "", source=r["source"] or "",
        )
        for r in rows
    ]


def _flush_summaries(rows: list[dict]) -> None:
    if rows:
        graph._run(
            """
            UNWIND $rows AS row
            MATCH (s:Symbol {id: row.id})
            SET s.summary = row.summary
            """,
            rows=rows,
        )


def write_summaries(workers: int = 8, *, progress_every: int = 500) -> dict[str, int]:
    """Summarise every symbol still missing a summary, in parallel.

    Results are flushed in batches as they complete, so a crash loses at most one
    batch — never the whole run. Failures are left pending and retried by simply
    running this again.
    """
    todo = pending_summaries()
    total = len(todo)
    if not total:
        print("[summarize] nothing pending")
        return {"summarized": 0, "failed": 0, "pending": 0}

    print(f"[summarize] {total:,} pending, {workers} workers")
    done = failed = 0
    buffer: list[dict] = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(summarize.summarize_symbol, s): s for s in todo}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                text = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[summarize] FAILED {sym.id}: {exc}")
                text = None
            if text:
                buffer.append({"id": sym.id, "summary": text})
                done += 1
            else:
                failed += 1
            if len(buffer) >= _BATCH:
                _flush_summaries(buffer)
                buffer = []
            if (done + failed) % progress_every == 0:
                _report(done + failed, total, started)
    _flush_summaries(buffer)

    _report(done + failed, total, started)
    print(f"[summarize] done: {done:,} written, {failed:,} failed (rerun to retry)")
    return {"summarized": done, "failed": failed, "pending": failed}


def _report(seen: int, total: int, started: float) -> None:
    elapsed = max(0.001, time.time() - started)
    rate = seen / elapsed
    eta = (total - seen) / rate if rate else 0
    print(
        f"[summarize] {seen:,}/{total:,} ({100*seen/total:.1f}%) "
        f"{rate:.1f}/s  eta {eta/60:.0f}m",
        flush=True,
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
    failed = 0
    for cid, members in communities.items():
        member_summaries = [summaries_by_symbol.get(m, m) for m in members]
        name = f"Community {cid}"
        summary = summarize.summarize_community(name, member_summaries)
        if summary == name:  # the fallback fired — the LLM call failed
            failed += 1
        community_rows.append(
            {"id": cid, "name": name, "summary": summary, "member_count": len(members)}
        )
    if failed:
        print(
            f"[index] WARNING: {failed}/{len(communities)} community summaries failed; "
            f"list_communities will be degraded. Rerun: --stage community"
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


def write_profile(prof) -> None:
    """Persist the measured scale profile so query-time code can adapt to it
    without re-measuring (agent step budget, render strategy, tool limits)."""
    graph._run(
        """
        MERGE (m:RepoMeta {id: 'current'})
        SET m += $props
        """,
        props=prof.as_dict(),
    )


def load_call_edges() -> list[Edge]:
    """Read the CALLS graph back from Neo4j so community detection can run as a
    standalone stage — without re-parsing 2M LOC."""
    rows = graph._run(
        """
        MATCH (a:Symbol)-[r:CALLS]->(b:Symbol)
        RETURN a.id AS src, b.id AS dst, coalesce(r.count, 1) AS count
        """
    )
    return [Edge(r["src"], r["dst"], "CALLS", count=r["count"]) for r in rows]


def load_summaries() -> dict[str, str]:
    rows = graph._run(
        "MATCH (s:Symbol) WHERE s.summary IS NOT NULL RETURN s.id AS id, s.summary AS summary"
    )
    return {r["id"]: r["summary"] for r in rows}


def counts() -> tuple[int, int]:
    n = graph._run("MATCH (s:Symbol) RETURN count(s) AS n")[0]["n"]
    e = graph._run("MATCH (:Symbol)-[r]->(:Symbol) RETURN count(r) AS n")[0]["n"]
    return n, e


STAGES = ("parse", "summarize", "community")


def index_repo(
    repo_path: str,
    *,
    fresh: bool = False,
    stages: tuple[str, ...] = STAGES,
    workers: int | None = None,
) -> dict[str, Any]:
    """Index a repo. Every stage is idempotent and independently runnable.

    On a 2M-LOC codebase this is a multi-hour job, so it is built to be resumed
    rather than restarted: `parse` MERGEs, `summarize` reads its work queue from
    the graph, and `community` reads the CALLS graph back. Re-running after a
    crash continues from where it stopped.

    `fresh=True` wipes first — the ONLY destructive path, and it is opt-in
    precisely so a crash at hour 15 does not leave you with nothing.
    """
    result: dict[str, Any] = {}
    print("[index] schema setup")
    graph.setup_schema()

    if fresh:
        print("[index] fresh=True -> wiping existing graph")
        graph.clear_graph()

    if "parse" in stages:
        print(f"[index] parsing {repo_path}")
        t0 = time.time()
        symbols, edges = parse_repo(repo_path)
        print(f"[index] {len(symbols):,} symbols, {len(edges):,} edges "
              f"in {time.time()-t0:.0f}s")
        write_symbols(symbols)
        write_edges(edges)
        result |= {"symbols": len(symbols), "edges": len(edges)}
        del symbols, edges  # release before the long LLM stage

    if "summarize" in stages:
        n_sym, _ = counts()
        prof = scale.derive(n_sym, 0)
        print("[index] summarizing symbols (LLM)")
        result |= write_summaries(workers or prof.summary_workers)

    if "community" in stages:
        edges = load_call_edges()
        n_sym, n_edge = counts()
        prof = scale.derive(n_sym, n_edge)
        print(f"[index] scale profile: {prof.explain()}")
        print("[index] Leiden community detection")
        assignments, communities = community_mod.detect_communities(
            [], edges, target_communities=prof.fine_communities
        )
        print(f"[index] {len(communities):,} communities")
        write_communities(assignments, communities, load_summaries())
        write_profile(prof)
        result |= {"communities": len(communities), "tier": prof.tier}

    print("[index] done")
    n_sym, n_edge = counts()
    return result | {"total_symbols": n_sym, "total_edges": n_edge}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m indexer",
        description="Index a repo into the graph. Stages are resumable — rerun "
                    "after a crash and it continues where it stopped.",
    )
    ap.add_argument("repo_path")
    ap.add_argument("--fresh", action="store_true",
                    help="WIPE the graph before indexing (destructive; off by default)")
    ap.add_argument("--stage", action="append", choices=STAGES, dest="stages",
                    help="run only this stage; repeatable. Default: all")
    ap.add_argument("--workers", type=int, default=None,
                    help="summariser concurrency (default: from the scale profile)")
    args = ap.parse_args()

    stats = index_repo(
        args.repo_path,
        fresh=args.fresh,
        stages=tuple(args.stages) if args.stages else STAGES,
        workers=args.workers,
    )
    print(stats)
    graph.close_driver()


if __name__ == "__main__":
    main()
