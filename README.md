# Codebase Explorer

Point it at a repo; it builds a graph of every file, class, and function and the
relationships between them (imports, calls, inheritance), then lets you ask
questions in plain English ("how does authentication work?") and get an answer
**plus a highlighted path through the code**.

Runs **completely locally via Docker**. The only thing that leaves the machine is
the LLM API call (your own Anthropic or OpenAI key). No embedding model, no vector
database — retrieval is lexical full-text search over LLM-generated summaries plus
deterministic graph traversal over AST-derived edges. See [CLAUDE.md](CLAUDE.md)
for the full design.

## Quick start

```bash
cp .env.example .env        # add your ANTHROPIC_API_KEY (or set LLM_PROVIDER=openai)

# 1. Start the graph DB
docker compose up -d neo4j

# 2. Drop a repo into ./repos and index it
docker compose run --rm api python -m indexer /repos/<repo-name>

#    Indexing is STAGED and RESUMABLE — on a large codebase this is a multi-hour
#    job, so a crash resumes instead of restarting. Nothing is destroyed unless
#    you pass --fresh.
#      --stage parse|summarize|community   run one stage (repeatable)
#      --workers N                         summariser concurrency
#      --fresh                             WIPE first (destructive, opt-in)
#
#    Interrupted? Just run it again — completed work is skipped.

# 3. Bring up the app
docker compose up -d
#   web           → http://localhost:3000
#   api           → http://localhost:8000
#   neo4j browser → http://localhost:7474   (inspect the graph while debugging)
```

Ask a question in the chat panel; the traced symbols and the edges between them
light up in the graph.

## Layout

```
api/   FastAPI backend — parsing, graph, summaries, Leiden, the agent loop
web/   React + Cytoscape.js frontend (graph + chat, lit-path highlighting)
repos/ drop target codebases here (read-only mount)
```

## Languages

| Language | Extensions | Extracts |
|---|---|---|
| Python | `.py` | classes, functions, methods, imports, calls, inheritance |
| C# | `.cs` | namespaces, types, members, calls, inheritance, partial-class merging, WebForms classification, legacy-API usage, stored-procedure references |
| WebForms markup | `.aspx` `.ascx` `.master` | pages, `HANDLES` edges to event handlers and lifecycle methods |

Add one by dropping a `parsing/<lang>_ext.py` in and registering it — see
CLAUDE.md §10. For the .NET WebForms → .NET Core migration build, see
[PRODUCTION-DOTNET.md](PRODUCTION-DOTNET.md).

## Measure before you index

Point `measure` at ONE real module first. It costs nothing — no LLM calls, no
database — and reports what the extractor actually got, chiefly the **call
resolution rate**: the fraction of call sites that became `CALLS` edges.
Unresolved calls are dropped rather than guessed, so a low rate means blast
radius silently under-reports.

```bash
# from the project root — works with any host path
venv/bin/python api/measure.py /path/to/YourModule

# or in the container (paths must live under ./repos, mounted at /repos)
cp -r /path/to/YourModule ./repos/
docker compose run --rm --no-deps api python -m measure /repos/YourModule
```

`python -m measure` only works when `measure.py` is in the current directory
(i.e. from `api/`, or inside the container where it is `/app`). Running it by
file path works from anywhere.

## Development

```bash
docker compose run --rm api pytest        # tests
docker compose run --rm api ruff check .  # lint
docker compose run --rm api ruff format . # format
```

Integration tests exercise the real Cypher against a live Neo4j and skip when one
isn't configured:

```bash
docker run -d --name neo4j-test -p 7688:7687 -e NEO4J_AUTH=neo4j/testpassword123 neo4j:5-community
NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=testpassword123 pytest
```

The Python backend targets **3.12** (see `api/Dockerfile`). `tree-sitter` is pinned
to 0.21.x because `tree-sitter-languages` only links against that C API.

## Status

Phase 1 (MVP): Python and C#/WebForms, end-to-end index → ask → highlighted path.
Cross-file call resolution beyond same-file + import-level targets, multi-language,
and incremental re-index are later phases (CLAUDE.md §12).
