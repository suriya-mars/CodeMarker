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

import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

import community as community_mod
import graph
import guard
import scale
import summarize
from parsing import Edge, Symbol, parse_repo

# Relationship types we are allowed to write. Cypher rel-types can't be
# parameterized, so we dispatch per (validated) type — never interpolate raw input.
_EDGE_TYPES = {"CONTAINS", "IMPORTS", "CALLS", "INHERITS", "HANDLES",
               "EXECUTES", "TRIGGERS"}
_BATCH = 500


def _batches(items, size=_BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def write_symbols(symbols: list[Symbol], *, repo: str = "", run_id: str = "") -> None:
    """Write symbols, preserving summaries for code that did not change.

    Two things here are load-bearing for re-indexing after a teammate pushes:

    * `summary` is REMOVED from the row. The parse-time dataclass carries
      `summary=""`, and `SET s += row` would clobber the real summary with it —
      making every re-index re-summarise the whole repo at full cost.
    * The summary is cleared ONLY when `source` actually changed, so edited code
      is re-summarised and untouched code is not.

    `repo` and `run_id` tag each node so `delete_stale()` can remove symbols for
    code that no longer exists without touching other repos in the same graph.
    """
    rows = []
    for sym in symbols:
        row = asdict(sym)
        row.pop("summary", None)  # owned by write_summaries, never by parsing
        row["repo"] = repo
        row["last_seen"] = run_id
        rows.append(row)

    for batch in _batches(rows):  # 500 rows per round trip
        graph._run(
            """
            UNWIND $rows AS row
            MERGE (s:Symbol {id: row.id})
            WITH s, row, (s.source IS NOT NULL AND s.source <> row.source) AS changed
            SET s += row
            SET s.summary = CASE WHEN changed THEN null ELSE s.summary END
            """,
            rows=batch,
        )


def delete_stale(repo: str, run_id: str, keep_paths: list[str] | None = None) -> int:
    """Remove symbols AND parsed edges of this repo that the latest parse did
    not see.

    MERGE only adds and updates, so without this a deleted method lingers in the
    graph forever and blast radius reports dependencies on code that is gone.
    Edges get the same treatment: when `a()` stops calling `b()` and both still
    exist, the old CALLS edge would otherwise survive every re-index and keep
    `a` in `b`'s blast radius. Scoped by `repo` so indexing one codebase never
    disturbs another sharing the same database.

    `keep_paths` are files that FAILED to parse this run. Their symbols were not
    seen, but the code still exists — deleting them would turn a transient parse
    error into silently missing code (and re-paid summaries).
    """
    if not repo or not run_id:
        return 0
    keep = keep_paths or []
    rel_types = "|".join(sorted(_EDGE_TYPES))  # validated enum — safe to interpolate
    graph._run(
        f"""
        MATCH (a:Symbol {{repo: $repo}})-[r:{rel_types}]->(b:Symbol)
        WHERE (r.last_seen IS NULL OR r.last_seen <> $run_id)
          AND NOT any(p IN split(a.path, ';') + split(b.path, ';') WHERE p IN $keep)
        DELETE r
        """,
        repo=repo,
        run_id=run_id,
        keep=keep,
    )
    rows = graph._run(
        """
        MATCH (s:Symbol {repo: $repo})
        WHERE (s.last_seen IS NULL OR s.last_seen <> $run_id)
          AND NOT any(p IN split(s.path, ';') WHERE p IN $keep)
        WITH s, count(*) AS _
        DETACH DELETE s
        RETURN count(*) AS removed
        """,
        repo=repo,
        run_id=run_id,
        keep=keep,
    )
    return rows[0]["removed"] if rows else 0


def write_edges(edges: list[Edge], *, run_id: str = "") -> None:
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
            SET r.count = row.count, r.last_seen = $run_id
        """
        for batch in _batches(rows):
            graph._run(cypher, rows=batch, run_id=run_id)


def pending_summaries(limit: int | None = None, repo: str = "") -> list[Symbol]:
    """Symbols still missing a summary, read back from the graph.

    Reading the work queue from Neo4j rather than from the in-memory symbol list
    is what makes summarisation **resumable**: a crashed 21-hour run resumes
    exactly where it stopped, and re-running is idempotent. Everything already
    summarised is simply not returned.
    """
    rows = graph._run(
        f"""
        MATCH (s:Symbol)
        WHERE (s.summary IS NULL OR s.summary = '') AND ($repo = '' OR s.repo = $repo)
        RETURN s.id AS id, s.kind AS kind, s.name AS name, s.path AS path,
               s.language AS language, s.signature AS signature,
               s.source AS source, s.start_line AS start_line, s.end_line AS end_line
        {f'LIMIT {int(limit)}' if limit else ''}
        """,
        repo=repo,
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


_SUMMARY_CACHE_DAYS = 30


def _flush_summaries(rows: list[dict]) -> None:
    """Write summaries to their symbols, and remember each by its summary key."""
    if not rows:
        return
    graph._run(
        """
        UNWIND $rows AS row
        MATCH (s:Symbol {id: row.id})
        SET s.summary = row.summary
        """,
        rows=rows,
    )
    keyed = list({r["key"]: {"key": r["key"], "summary": r["summary"]}
                  for r in rows if r.get("key")}.values())
    if keyed:
        graph._run(
            """
            UNWIND $rows AS row
            MERGE (c:SummaryCache {key: row.key})
            SET c.summary = row.summary, c.last_used = $now
            """,
            rows=keyed, now=time.time(),
        )


def _cached_summaries(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = graph._run(
        "MATCH (c:SummaryCache) WHERE c.key IN $keys RETURN c.key AS key, c.summary AS summary",
        keys=keys,
    )
    return {r["key"]: r["summary"] for r in rows}


def _prune_summary_cache() -> None:
    """Entries are kept a while after their code disappears, because code that was
    deleted or moved often comes back (a revert, a branch merged later)."""
    graph._run("MATCH (c:SummaryCache) WHERE c.last_used < $cutoff DELETE c",
               cutoff=time.time() - _SUMMARY_CACHE_DAYS * 86400)


def write_summaries(workers: int = 8, *, progress_every: int = 500, repo: str = "",
                    on_progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """Summarise every symbol still missing a summary, in parallel.

    Identical input is never paid for twice (summarize.summary_key): a summary
    already in the cache is reused, and symbols with the same input in this run
    share one call. Results are flushed in batches as they complete, so a crash
    loses at most one batch, never the whole run. Failures are left pending and
    retried by simply running this again; they are never cached.
    """
    todo = pending_summaries(repo=repo)
    total = len(todo)
    if not total:
        print("[summarize] nothing pending")
        return {"summarized": 0, "reused": 0, "failed": 0, "pending": 0}
    step = max(1, total // 50)  # about 50 progress updates, however large the repo

    groups: dict[str, list[Symbol]] = {}
    for sym in todo:
        groups.setdefault(summarize.summary_key(sym), []).append(sym)
    known = _cached_summaries(list(groups))
    reused_rows = [{"id": sym.id, "summary": known[key], "key": key}
                   for key in known for sym in groups.pop(key)]
    for i in range(0, len(reused_rows), _BATCH):
        _flush_summaries(reused_rows[i:i + _BATCH])
    reused = len(reused_rows)

    calls = len(groups)
    print(f"[summarize] {total:,} pending: {reused:,} from the cache, "
          f"{calls:,} to summarize ({workers} workers)")
    done = failed = 0
    processed = reused  # symbols with a summary decided, cached or not
    buffer: list[dict] = []
    started = time.time()
    if on_progress and reused:
        on_progress(reused, total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(summarize.summarize_symbol, members[0]): (key, members)
                   for key, members in groups.items()}
        for fut in as_completed(futures):
            key, members = futures[fut]
            try:
                text = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[summarize] FAILED {members[0].id}: {exc}")
                text = None
            if text:
                buffer += [{"id": m.id, "summary": text, "key": key} for m in members]
                done += 1
                reused += len(members) - 1  # same input elsewhere in this run: no extra call
            else:
                failed += len(members)
            if len(buffer) >= _BATCH:
                _flush_summaries(buffer)
                buffer = []
            before, processed = processed, processed + len(members)
            if processed // progress_every > before // progress_every:
                _report(processed, total, started)
            if on_progress and processed // step > before // step:
                on_progress(processed, total)
    _flush_summaries(buffer)
    _prune_summary_cache()
    _report(processed, total, started)

    print(f"[summarize] done: {done:,} summarized, {reused:,} reused, "
          f"{failed:,} failed (rerun to retry)")
    return {"summarized": done, "reused": reused, "failed": failed, "pending": failed}


def _report(seen: int, total: int, started: float) -> None:
    elapsed = max(0.001, time.time() - started)
    rate = seen / elapsed
    eta = (total - seen) / rate if rate else 0
    print(
        f"[summarize] {seen:,}/{total:,} ({100*seen/total:.1f}%) "
        f"{rate:.1f}/s  eta {eta/60:.0f}m",
        flush=True,
    )


_COMMUNITY_CACHE_DAYS = 30


def _cached_community_summaries(fingerprints: list[str]) -> dict[str, str]:
    rows = graph._run(
        """
        MATCH (c:CommunitySummary) WHERE c.fingerprint IN $fingerprints
        RETURN c.fingerprint AS fingerprint, c.summary AS summary
        """,
        fingerprints=fingerprints,
    )
    return {r["fingerprint"]: r["summary"] for r in rows}


def _store_community_summary(fingerprint: str, summary: str) -> None:
    graph._run(
        """
        MERGE (c:CommunitySummary {fingerprint: $fingerprint})
        SET c.summary = $summary, c.last_used = $now
        """,
        fingerprint=fingerprint, summary=summary, now=time.time(),
    )


def _touch_community_summaries(fingerprints: list[str]) -> None:
    """Keep entries in use, and drop those no community has matched in a while.
    Old entries are kept for a time because a community can split and later return."""
    now = time.time()
    graph._run(
        """
        MATCH (c:CommunitySummary) WHERE c.fingerprint IN $fingerprints
        SET c.last_used = $now
        """,
        fingerprints=fingerprints, now=now,
    )
    graph._run(
        "MATCH (c:CommunitySummary) WHERE c.last_used < $cutoff DELETE c",
        cutoff=now - _COMMUNITY_CACHE_DAYS * 86400,
    )


def summarize_communities(
    communities: dict[str, list[str]], summaries_by_symbol: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Summarise only the communities whose prompt input changed.

    Each new summary is stored the moment it is paid for, so a crash partway loses
    nothing and the rerun continues from there. A failed call is never cached: the
    next run retries it. Runs before any destructive write in write_communities, so
    a crash here leaves the previous communities intact.
    """
    shown = {cid: summarize.community_prompt_members(members, summaries_by_symbol)
             for cid, members in communities.items()}
    fingerprints = {cid: summarize.community_fingerprint(s) for cid, s in shown.items()}
    known = _cached_community_summaries(sorted(set(fingerprints.values())))
    rows: list[dict[str, Any]] = []
    generated = reused = failed = 0
    for cid, members in communities.items():
        name = f"Community {cid}"
        fingerprint = fingerprints[cid]
        summary = known.get(fingerprint)
        if summary is not None:
            reused += 1
        else:
            summary = summarize.summarize_community(name, [text for _, text in shown[cid]])
            if summary == name:  # the fallback fired: the LLM call failed
                failed += 1
            else:
                generated += 1
                known[fingerprint] = summary  # identical communities pay once
                _store_community_summary(fingerprint, summary)
        rows.append({"id": cid, "name": name, "summary": summary, "member_count": len(members)})
    _touch_community_summaries(sorted(set(fingerprints.values())))
    print(f"[index] community summaries: {generated:,} generated, {reused:,} reused, "
          f"{failed:,} failed")
    if failed:
        print(
            f"[index] WARNING: {failed}/{len(communities)} community summaries failed; "
            f"list_communities will be degraded. Rerun: --stage community"
        )
    return rows, {"community_summaries_generated": generated,
                  "community_summaries_reused": reused,
                  "community_summaries_failed": failed}


def write_communities(
    assignments: dict[str, str],
    communities: dict[str, list[str]],
    summaries_by_symbol: dict[str, str],
) -> dict[str, int]:
    if not communities:
        return {}
    community_rows, stats = summarize_communities(communities, summaries_by_symbol)
    # Leiden renumbers on every run, and MERGE only adds: without this, symbols keep
    # memberships from earlier runs and deleted code leaves stale communities listed.
    graph._run("MATCH (:Symbol)-[r:IN_COMMUNITY]->(:Community) DELETE r")
    graph._run("MATCH (s:Symbol) WHERE s.community_id IS NOT NULL REMOVE s.community_id")
    graph._run("MATCH (c:Community) WHERE NOT c.id IN $ids DETACH DELETE c",
               ids=list(communities))
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
    return stats


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


def record_commit(repo: str, repo_path: str) -> None:
    """Remember which commit the graph reflects. Impact analysis diffs from it:
    the blast radius is exact only when the diff base IS the indexed commit."""
    import impact  # local: impact imports parsing/graph, not indexer

    commit = impact.git_head(repo_path)
    if commit:
        graph._run(
            "MERGE (r:RepoState {repo: $repo}) SET r.commit = $commit, r.indexed_at = $t",
            repo=repo, commit=commit, t=time.time(),
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
    repo: str = "",
    fresh: bool = False,
    stages: tuple[str, ...] = STAGES,
    workers: int | None = None,
    progress: Callable[..., None] | None = None,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    """Index a repo. Every stage is idempotent and independently runnable.

    On a 2M-LOC codebase this is a multi-hour job, so it is built to be resumed
    rather than restarted: `parse` MERGEs, `summarize` reads its work queue from
    the graph, and `community` reads the CALLS graph back. Re-running after a
    crash continues from where it stopped.

    `fresh=True` wipes first — the ONLY destructive path, and it is opt-in
    precisely so a crash at hour 15 does not leave you with nothing.

    `progress(stage, done, total)` reports where a background job is (repo_jobs).
    `max_symbols` refuses a repo larger than that BEFORE anything is written or any
    summary is paid for.
    """
    result: dict[str, Any] = {}
    report = progress or (lambda *a, **k: None)
    repo_name = repo or os.path.basename(os.path.normpath(repo_path))
    print("[index] schema setup")
    graph.setup_schema()

    if fresh:
        removed = graph.clear_code_graph(repo_name)
        print(f"[index] fresh=True -> deleted {removed:,} symbols of {repo_name}; "
              f"other repos and user data are kept")

    if "parse" in stages:
        report("parsing")
        run_id = uuid.uuid4().hex
        print(f"[index] parsing {repo_path}  (repo={repo_name})")
        t0 = time.time()
        stats: dict[str, Any] = {}
        symbols, edges = parse_repo(repo_path, stats)
        print(f"[index] {len(symbols):,} symbols, {len(edges):,} edges "
              f"in {time.time()-t0:.0f}s")
        if max_symbols and len(symbols) > max_symbols:
            raise RuntimeError(
                f"The repository has {len(symbols):,} classes and functions, over the "
                f"limit of {max_symbols:,} (INDEX_MAX_SYMBOLS). Indexing it would pay "
                f"for a summary of each one.")
        write_symbols(symbols, repo=repo_name, run_id=run_id)
        write_edges(edges, run_id=run_id)
        if stats["skipped"]:
            print(f"[index] skipped {len(stats['skipped']):,} generated or excluded file(s)")
        if stats["failed_paths"]:
            print(f"[index] {len(stats['failed_paths'])} file(s) failed to parse; "
                  f"keeping their previously indexed symbols")
        removed = delete_stale(repo_name, run_id, keep_paths=stats["failed_paths"])
        if removed:
            print(f"[index] removed {removed:,} symbols for code that no longer exists")
        record_commit(repo_name, repo_path)
        result |= {"symbols": len(symbols), "edges": len(edges), "removed": removed,
                   "skipped_files": len(stats["skipped"])}
        del symbols, edges  # release before the long LLM stage

    if "summarize" in stages:
        guard.check_config()  # summaries are where injected code is first read
        guard.preload()
        n_sym, _ = counts()
        prof = scale.derive(n_sym, 0)
        print("[index] summarizing symbols (LLM)")
        report("summarizing")
        result |= write_summaries(workers or prof.summary_workers, repo=repo_name,
                                  on_progress=lambda done, total: report("summarizing", done, total))

    if "community" in stages:
        report("grouping")
        edges = load_call_edges()
        n_sym, n_edge = counts()
        prof = scale.derive(n_sym, n_edge)
        print(f"[index] scale profile: {prof.explain()}")
        print("[index] Leiden community detection")
        assignments, communities = community_mod.detect_communities(
            [], edges, target_communities=prof.fine_communities
        )
        print(f"[index] {len(communities):,} communities")
        result |= write_communities(assignments, communities, load_summaries())
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
                    help="delete THIS repo's code graph before indexing (off by default); "
                         "other repos and user data are kept")
    ap.add_argument("--stage", action="append", choices=STAGES, dest="stages",
                    help="run only this stage; repeatable. Default: all")
    ap.add_argument("--repo", default="",
                    help="repo name for scoping (default: the directory name)")
    ap.add_argument("--workers", type=int, default=None,
                    help="summariser concurrency (default: from the scale profile)")
    args = ap.parse_args()

    stats = index_repo(
        args.repo_path,
        repo=args.repo,
        fresh=args.fresh,
        stages=tuple(args.stages) if args.stages else STAGES,
        workers=args.workers,
    )
    print(stats)
    graph.close_driver()


if __name__ == "__main__":
    main()
