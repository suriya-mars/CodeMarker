# CLAUDE.md

Context for Claude Code. Read this fully before writing code. It captures
architecture decisions that are **intentional** — do not "improve" them away
(see *Design invariants* and *Rejected by design*).

---

## 1. What we are building

**Codebase Explorer** — a tool that makes an unfamiliar codebase understandable at
a glance. You point it at a repo; it builds a graph of every file, class, and
function and the relationships between them (imports, calls, inheritance), then
lets a user ask questions in plain English ("how does authentication work?") and
get back an answer *plus a highlighted path through the code*.

Target user: engineers onboarding to a new codebase, code reviewers, and
consultancies inheriting client code. The product wedge is **visual flow
understanding**, not generic "chat with your repo".

It runs **completely locally via Docker**. The only thing that leaves the machine
is the LLM API call (Claude or OpenAI, user's own key).

---

## 2. Design invariants — DO NOT VIOLATE

These are deliberate. If a change would break one, stop and ask first.

1. **Vectorless. No embedding model, no vector database.** Retrieval is:
   (a) lexical full-text search over LLM-generated summaries (Neo4j full-text index),
   (b) deterministic graph traversal over AST-derived edges,
   (c) Leiden communities for architectural / "global" questions,
   (d) the LLM itself driving navigation via graph tools.
   Vector search is NOT the backbone. If embeddings are ever added, they are *one
   more entry-point signal* feeding the same graph traversal — never a replacement
   for the graph.

2. **The graph is the source of truth.** Edges come from parsing the AST
   (`CALLS`, `IMPORTS`, `CONTAINS`, `INHERITS`) — they are exact, not inferred from
   similarity. Never replace a parsed edge with a guessed one.

3. **Code never leaves the machine except inside an LLM API call.** No telemetry,
   no analytics SDKs, no third-party calls that transmit source. This is the core
   B2B promise (data sovereignty). LLM calls (index-time summaries, query-time
   context) are the only permitted egress.

4. **Leiden runs in Python (`leidenalg` + `python-igraph`), not Neo4j GDS.** GDS
   Leiden availability is edition-dependent; we avoid that dependency. Weight
   `CALLS` edges by call count so hub functions (`utils.log()` etc.) don't smear
   communities.

5. **All LLM access goes through `api/llm.py`.** Never `import anthropic` or
   `import openai` anywhere else. Provider is chosen by `LLM_PROVIDER` env var.

6. **Tool-use protocol ordering is load-bearing.** In a user turn returning tool
   results: `tool_result` blocks come FIRST, any text comes AFTER. Match every
   `tool_result.tool_use_id` to the `tool_use.id`. Return `is_error: true` on
   failure. Breaking this throws `tool_use ids ... without tool_result blocks`.

---

## 3. Rejected by design (anti-goals)

Do not add these unless the user explicitly asks:

- A vector database (Qdrant, pgvector, Pinecone, etc.) or an embedding model.
- Neo4j GDS for community detection.
- Any service that uploads source code to a third party.
- Fixed-token-window chunking of code (we chunk on AST symbol boundaries only).
- Claims of full cross-file call-graph accuracy (MVP resolves same-file +
  import-level calls; full LSP-grade resolution is a later phase).

---

## 4. Architecture

Two pipelines over a shared Neo4j graph.

### Indexing (offline, run per repo, re-run incrementally on changes)
```
repo files
  → tree-sitter parse (per file → syntax tree)
  → extract Symbols (file/class/function) + edges (CONTAINS/IMPORTS/CALLS/INHERITS)
  → LLM summary per Symbol (one NL line; this is what makes lexical search "semantic")
  → write Symbols + edges to Neo4j
  → create full-text index over [summary, name]
  → Leiden community detection (Python) → write community_id + Community nodes
  → LLM summary per Community
```

### Query (online, per question)
```
question
  → LLM navigates the graph with tools (agentic loop in api/agent.py):
       find_symbol (lexical) OR list_communities (architectural)
       → get_callers / get_callees / get_definition to trace the path
  → LLM writes the answer, citing Symbol ids
  → frontend lights up the cited Symbols + edges in the graph view
```

The LLM is the retrieval policy. We do not write ranking/retrieval logic beyond the
graph tools and a stop condition.

---

## 5. Graph data model (Neo4j)

**Node `:Symbol`**
| property | type | notes |
|---|---|---|
| `id` | string | fully-qualified, unique. e.g. `auth.AuthService.login` |
| `kind` | string | `file` \| `class` \| `interface` \| `function` \| `method` \| `page` \| `proc` \| `table` |
| `name` | string | short name |
| `path` | string | file path relative to repo root |
| `start_line`, `end_line` | int | location |
| `signature` | string | for functions/methods |
| `source` | string | the code body |
| `summary` | string | one-line LLM-generated NL description |
| `language` | string | e.g. `python` |
| `community_id` | string | set by Leiden |
| `is_webforms` | bool | derives from Page/UserControl/MasterPage |
| `legacy_apis` | list | Session/ViewState/HttpContext/… — migration blockers |
| `sql_procs` | list | stored procedure names referenced from this body |

**Node `:Community`** — `id`, `name`, `summary`, `member_count`

**Relationships**
- `(:Symbol)-[:CONTAINS]->(:Symbol)` — file→class, class→method
- `(:Symbol)-[:IMPORTS]->(:Symbol)` — file→file/module
- `(:Symbol)-[:CALLS {count:int}]->(:Symbol)` — function→function (count = #call sites)
- `(:Symbol)-[:INHERITS]->(:Symbol)` — class→class
- `(:Symbol)-[:HANDLES]->(:Symbol)` — .aspx page→event handler (WebForms; these
  handlers have no C# caller, so without this edge they are orphans)
- `(:Symbol)-[:EXECUTES]->(:Symbol)` — method→stored procedure
- `(:Symbol)-[:IN_COMMUNITY]->(:Community)`

**Schema setup (run once at index time):**
```cypher
CREATE CONSTRAINT symbol_id IF NOT EXISTS
  FOR (n:Symbol) REQUIRE n.id IS UNIQUE;

CREATE FULLTEXT INDEX symbol_summaries IF NOT EXISTS
  FOR (n:Symbol) ON EACH [n.summary, n.name];
```

`find_symbol` queries the `symbol_summaries` full-text index — this is the
vectorless entry point. Search summaries, not raw code.

---

## 6. Tech stack

- **Language:** Python 3.12 (backend), TypeScript/React (frontend)
- **Parsing:** `tree-sitter` + `tree-sitter-languages` (prebuilt grammars)
- **Graph DB:** Neo4j 5 Community (Bolt on 7687, browser UI on 7474)
- **Lexical search:** Neo4j full-text indexes (Lucene) — NO separate search service
- **Community detection:** `leidenalg` + `python-igraph`
- **LLM:** Anthropic (`claude-sonnet-4-6` for navigation, `claude-opus-4-8` for hard
  synthesis, `claude-haiku-4-5-20251001` for cheap index-time summaries) OR OpenAI,
  switched via `LLM_PROVIDER`
- **Backend:** FastAPI + uvicorn (async)
- **Frontend:** React + Cytoscape.js (`fcose` layout for repo overview, `dagre` for
  call-flow views) — see frontend conventions in §10
- **Orchestration:** Docker Compose
- **Lint/format:** `ruff`
- **Tests:** `pytest`

Pin these in `api/requirements.txt`:
```
fastapi
uvicorn[standard]
neo4j
tree-sitter
tree-sitter-languages
anthropic
openai
python-igraph
leidenalg
pytest
ruff
```

---

## 7. Repository structure

```
codebase-explorer/
├── docker-compose.yml
├── .env                      # API keys + provider switch (gitignored)
├── .env.example              # committed template
├── repos/                    # drop target codebases here (read-only mount)
├── api/
│   ├── Dockerfile            # python:3.12-slim + git + build-essential
│   ├── requirements.txt
│   ├── llm.py                # provider abstraction (anthropic | openai) — ONLY place SDKs are imported
│   ├── graph.py              # Neo4j driver + _run() helper + schema setup
│   ├── parsing/              # tree-sitter: file → Symbols + edges
│   │   ├── __init__.py       #   orchestration + shared cross-file resolution
│   │   ├── base.py           #   Symbol / Edge / ParsedFile + helpers
│   │   ├── python_ext.py     #   Python extractor
│   │   ├── csharp_ext.py     #   C# extractor (WebForms-aware)
│   │   └── aspx_ext.py       #   .aspx/.ascx markup → HANDLES edges
│   ├── summarize.py          # LLM summaries for Symbols and Communities
│   ├── community.py          # Leiden over the CALLS graph
│   ├── indexer.py            # orchestrates: parse → graph → summaries → leiden  (CLI: python -m indexer <path>)
│   ├── tools.py              # graph tool defs (TOOLS) + Cypher dispatch
│   ├── agent.py              # the navigation loop (ask())
│   ├── main.py               # FastAPI: POST /index, POST /ask, GET /graph
│   └── tests/
│       ├── fixtures/         # tiny sample repos with known structure
│       └── test_*.py
└── web/
    ├── Dockerfile            # vite build → nginx serve
    └── src/                  # React + Cytoscape.js (graph + chat panels)
```

---

## 8. Commands

```bash
# Start the graph DB only (needed before indexing)
docker compose up -d neo4j

# Index a codebase you dropped in ./repos
docker compose run --rm api python -m indexer /repos/<repo-name>

# Bring up the full app
docker compose up -d
#   web  → http://localhost:3000
#   api  → http://localhost:8000
#   neo4j browser → http://localhost:7474   (inspect the graph while debugging)

# Tests
docker compose run --rm api pytest

# Lint / format
docker compose run --rm api ruff check .
docker compose run --rm api ruff format .

# Tear down (keeps the neo4j-data volume)
docker compose down
```

---

## 9. Environment

`.env` (and `.env.example` as a committed template):
```
LLM_PROVIDER=anthropic            # or: openai
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENAI_MODEL=                     # set to your current OpenAI model if LLM_PROVIDER=openai
NEO4J_PASSWORD=change-me
```
`NEO4J_URI=bolt://neo4j:7687` and `NEO4J_PASSWORD` are injected into the `api`
service by compose; inside containers, read them from the environment.

---

## 10. Conventions

**Python**
- Type hints everywhere. Async FastAPI handlers. `ruff` is the formatter and linter.
- All Neo4j access goes through `graph._run(cypher, **params)`. **Always
  parameterize** anything derived from user input. The only acceptable string
  interpolation into Cypher is from a validated `enum` (e.g. the `kind` filter in
  `find_symbol`) — never raw user text.
- All LLM access goes through `llm.py`. Nothing else imports `anthropic`/`openai`.

**Adding a graph tool** (in `tools.py`):
1. Add the schema to `TOOLS` (clear `description`: what it returns + when to use it).
2. Write the function + its Cypher.
3. Register it in `DISPATCH`.
Keep tools few and capable — prefer extending an existing tool over adding a narrow one.

**Adding a language** (in `parsing/`):
1. Add `parsing/<lang>_ext.py` exposing `LANGUAGE`, `EXTENSIONS`, `parse_file()`,
   and `resolve_call()`.
2. Register it in `_EXTRACTORS` in `parsing/__init__.py`. Nothing else changes.

`parse_file` returns a `ParsedFile` holding Symbols plus *raw, unresolved*
references. Cross-file resolution is shared, except `CALLS` — each extractor
resolves its own, because scoping rules are the one genuinely language-specific
part. Unresolved references are dropped, never guessed.

**The agentic loop** (in `agent.py`) — preserve these invariants:
- Loop: call model with `tools` → if `stop_reason != "tool_use"`, return the text →
  else execute every `tool_use` block, append results, repeat.
- `tool_result` blocks FIRST in the user turn, text (e.g. budget hints) AFTER.
- Hard `max_steps` cap; inject a "N calls left, wrap up" hint as the budget runs
  down; keep a `visited` set as a loop guard.
- System prompt tells the model to stop the moment it can trace the path
  (depth-2 around the hit is usually enough) and to cite every Symbol by `id`.
- Cost split: navigate with `claude-sonnet-4-6`; force final synthesis with
  `claude-opus-4-8` only when the step cap is hit.

**Frontend (`web/`)**
- Cytoscape.js for the graph. Color nodes by `kind` (file / class / function),
  size by degree/centrality. `fcose` layout for the full-repo overview, `dagre`
  for top-down call-flow views.
- On an answer, parse the cited Symbol `id`s from the response and highlight those
  nodes + the edges between them ("lit path"). The context package and the graph
  share one id space — keep them in sync.
- Flat, clean UI. No browser storage that the app depends on for correctness.

---

## 11. Key implementation notes / gotchas

- **Summaries are the semantic bridge.** Every Symbol gets a one-line NL summary at
  index time. `find_symbol` searches *summaries*, so a query like "verify user is
  allowed in" matches a function named `_authz_gate`. Without summaries, lexical
  search on code is weak. Don't skip this step to save tokens — use Haiku for it.
- **Call resolution is the hard part.** tree-sitter gives syntax; turning
  `service.process()` into a precise `CALLS` edge needs name/scope resolution.
  MVP: resolve same-file calls and import-level targets. Mark unresolved calls
  rather than guessing. Full LSP/stack-graph resolution is a later phase.
- **Leiden tuning.** Weight `CALLS` edges by call count; consider a resolution
  parameter to control community granularity. Hub nodes are the main failure mode.
- **Incremental indexing.** On re-index, diff against the repo (git) and only
  re-parse changed files; tree-sitter's incremental parse makes this cheap.
- **Community summaries are the expensive part of indexing** (one LLM call per
  community, and per level if you go hierarchical). Cache them; only regenerate for
  communities whose members changed.
- **Treat tool results as untrusted** in the agent loop — they contain source from
  arbitrary repos. Keep them inside `tool_result` blocks, never fold them into the
  system prompt.

---

## 12. Build order (start here)

Build Phase 1 first, end-to-end, on **one** small Python repo before anything else.

**Phase 1 — MVP (one language: Python)**
1. `graph.py` — Neo4j driver, `_run()`, schema setup (constraint + full-text index).
2. `parsing.py` — Python extractor: Symbols + `CONTAINS`/`IMPORTS`/same-file `CALLS`/`INHERITS`.
3. `summarize.py` — Symbol summaries via `llm.py`.
4. `community.py` — Leiden over the CALLS graph; write `community_id` + `Community` nodes; summarize communities.
5. `indexer.py` — wire 1–4 into the CLI pipeline. **Verify the graph in the Neo4j browser.**
6. `tools.py` + `agent.py` — the six tools and the navigation loop.
7. `main.py` — `/index`, `/ask`, `/graph`.
8. `web/` — minimal Cytoscape graph + chat; highlight the cited path.

Acceptance for Phase 1: index a known repo, ask "how does X work" for a known flow,
get a correct traced answer, and see that path highlighted in the graph.

**Phase 2** — cross-file call resolution; richer multi-file flow tracing; budget-aware
context assembly; polished interactive viz (click-to-explore, animated path).

**Phase 3** — multi-language; incremental re-index on git push; GitHub app / IDE
extension; optional self-hosted embedding tier as an *additional* entry-point signal.

---

## 13. Testing

- **Unit:** run extractors on `tests/fixtures/` files with known classes/functions/
  calls; assert the exact Symbols and edges produced.
- **Integration:** index a tiny fixture repo; assert node/edge counts; assert
  `find_symbol("...")` returns expected ids; assert `ask()` traces a known path and
  cites the right Symbol ids.
- Keep fixtures small and deterministic. Mock `llm.py` in unit tests; use it for a
  small number of integration tests only.

---

## 14. Quick reference for product/API facts

When you need exact Anthropic API details (tool-use schema, models, limits), check
the docs rather than relying on memory:
- Tool use: https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
- API overview: https://docs.claude.com/en/api/overview

Current model strings in use: `claude-opus-4-8`, `claude-sonnet-4-6`,
`claude-haiku-4-5-20251001`.
