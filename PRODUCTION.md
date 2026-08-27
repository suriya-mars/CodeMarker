# PRODUCTION.md

Production readiness assessment for **Codebase Explorer**, and the work required to
get from the current state to something that can be run for real users.

Assessed at: 2026-08-24 · Codebase: 2,152 LOC (9 backend modules, 3 test files,
4 frontend modules, Docker Compose infra).

> **Read this alongside [CLAUDE.md](CLAUDE.md).** Nothing here proposes violating a
> design invariant (§2) or adding an anti-goal (§3). Everything below is hardening
> of the existing architecture, not a redesign.

> **Retargeting to .NET/SQL Server?** See [PRODUCTION-DOTNET.md](PRODUCTION-DOTNET.md)
> for the WebForms → .NET Core migration plan. Every P0 below still applies there.

---

## 1. Verdict

**Current state: a working Phase-1 MVP. Not production-ready.**

What genuinely works end-to-end today:

- tree-sitter Python extraction producing exact `CONTAINS` / `IMPORTS` / `CALLS` /
  `INHERITS` edges, with same-file and import-level call resolution
  ([parsing.py](api/parsing.py))
- Neo4j schema, constraints, and the full-text index that makes the vectorless
  approach work ([graph.py:48-67](api/graph.py#L48-L67))
- Leiden communities weighted by call count, in Python, as specified
  ([community.py](api/community.py))
- A correct Anthropic tool-use loop — `tool_result` first, ids matched, `is_error`
  on failure, budget hints, loop guard, Sonnet→Opus escalation
  ([agent.py:86-151](api/agent.py#L86-L151))
- Provider abstraction with a real OpenAI↔Anthropic message translation layer
  ([llm.py:165-257](api/llm.py#L165-L257))
- A Cytoscape graph with lit-path highlighting ([App.jsx](web/src/App.jsx))
- 8 passing tests, all green; ruff reports 2 trivial findings

The gap to production is **not** architectural. It is: no authentication on a
destructive API, an arbitrary-filesystem-path index endpoint, a dev container
configuration being used as the deployment configuration, a fully serial indexing
path that will not finish on a real repo, and no repo isolation in the graph.

**Estimated effort to P0-clear: 3–5 engineer-days. To P1-clear: 2–3 weeks.**

---

## 2. P0 — Blockers. Do not deploy to any shared host until these are closed.

### 2.1 `POST /index` is an unauthenticated arbitrary-path filesystem read

```python
# api/main.py:47-52
@app.post("/index")
def index(req: IndexRequest) -> dict[str, Any]:
    return indexer.index_repo(req.path)   # req.path is whatever the caller sent
```

`req.path` flows straight into `parse_repo()` → `os.walk()`. Any caller can walk
and ingest **any directory readable by the api container**, then read the ingested
source back out through `/ask` → `get_definition` → `s.source`. The container
mounts `./repos:ro`, but `/app` (the application source, bind-mounted at
[docker-compose.yml:33](docker-compose.yml#L33)) and the rest of the container
filesystem are in scope.

**Fix:** resolve the path and require it to live under a configured root.

```python
INDEX_ROOT = Path(os.environ.get("INDEX_ROOT", "/repos")).resolve()

def _validated_repo_path(raw: str) -> Path:
    p = (INDEX_ROOT / raw.lstrip("/")).resolve()
    if not p.is_relative_to(INDEX_ROOT) or not p.is_dir():
        raise HTTPException(400, "path must be a directory under the repos root")
    return p
```

Prefer accepting a **repo name**, not a path, and never echo the resolved path back
in the response.

### 2.2 `POST /index` destroys the entire graph, unauthenticated

```python
# api/indexer.py:136-137
graph.setup_schema()
graph.clear_graph()   # MATCH (n) DETACH DELETE n  — MVP: full re-index
```

One unauthenticated POST wipes every node and relationship in the database. This is
also a trivial denial-of-service: repeated calls keep the graph permanently empty
and burn LLM spend re-summarizing.

**Fix (three parts, all required):**
1. Put authentication in front of the API (§2.4).
2. Add repo scoping (§3.1) so a re-index deletes only that repo's subgraph:
   `MATCH (s:Symbol {repo: $repo}) DETACH DELETE s`.
3. Add a single-flight guard so two concurrent `/index` calls cannot interleave a
   `clear_graph()` into another run's writes (an `asyncio.Lock` is enough for one
   process; a Neo4j-backed lock node if you ever run more than one).

### 2.3 Neo4j is exposed on the host with a default password

```yaml
# docker-compose.yml:4-9
ports:
  - "7474:7474"   # browser UI
  - "7687:7687"   # bolt
environment:
  NEO4J_AUTH: "neo4j/${NEO4J_PASSWORD:-change-me}"
```

Combined with `graph.py:24`, which also defaults to `change-me`, an unset
`NEO4J_PASSWORD` yields a publicly-reachable graph database with a known password
containing the customer's source code — a direct breach of the data-sovereignty
promise in CLAUDE.md §2.3.

**Fix:**
- Delete both `change-me` defaults. Fail loudly at startup on a missing password:
  ```python
  password = os.environ["NEO4J_PASSWORD"]   # KeyError at boot beats a silent default
  ```
- In the production compose file, drop the `ports:` block entirely — `api` reaches
  Neo4j over the compose network at `bolt://neo4j:7687`; nothing needs it on the host.
- Bind to loopback (`127.0.0.1:7474:7474`) if you want the browser UI for debugging.
- Make the Neo4j username configurable (`graph.py:25` hardcodes `"neo4j"`).

### 2.4 No authentication anywhere, and CORS is fully open

```python
# api/main.py:21-26
allow_origins=["*"],  # local-only tool; the web container talks to this API
```

The comment's premise ("local-only") stops holding the moment this runs on a host
anyone else can reach. `allow_origins=["*"]` means any website a user visits can
drive `/ask` and `/index` against a reachable instance.

**Fix:** an API key or session check as a FastAPI dependency on every route, plus
`allow_origins` narrowed to the actual web origin, read from an env var. Even for
single-tenant on-prem deployments, one shared secret is the difference between
"internal tool" and "open to the office network".

### 2.5 The deployment config is the development config

```yaml
# docker-compose.yml:32-35
volumes:
  - ./api:/app              # host source shadows the image
command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The bind mount means the running code is whatever is on the host disk, not what was
built and tested into the image. `--reload` runs the autoreloader — extra processes,
file watchers, and a restart on any stray write. Neither belongs in production.

**Fix:** keep `docker-compose.yml` as the dev file, add
`docker-compose.prod.yml` that removes the bind mount and the `--reload` flag, adds
`restart: unless-stopped`, resource limits, and a healthcheck wired to the existing
`/health` endpoint ([main.py:42-44](api/main.py#L42-L44)):

```yaml
healthcheck:
  test: ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/health')\""]
  interval: 15s
  timeout: 5s
  retries: 5
```

Also add `web`'s `depends_on` a `condition: service_healthy` — today it is a bare
`depends_on` ([docker-compose.yml:44-45](docker-compose.yml#L44-L45)), so nginx can
start proxying to an api that isn't up.

### 2.6 Indexing is fully serial and will not finish on a real repo

```python
# api/indexer.py:69-73
for s in symbols:
    s.summary = summarize.summarize_symbol(s)   # one blocking LLM call, per symbol
```

One Haiku round-trip per symbol, sequentially, with no concurrency, batching, or
retry. At a realistic ~600 ms per call, a 5,000-symbol repo is **~50 minutes** of
wall time in a single blocking request. `POST /index` is synchronous, and
[nginx.conf:16](web/nginx.conf#L16) sets `proxy_read_timeout 300s` — so any repo
that takes over five minutes returns a gateway timeout to the browser while the
indexing thread keeps running and keeps spending.

**Fix (all three):**
1. **Make `/index` a job.** Return `202 Accepted` with a job id immediately; run the
   pipeline in a background worker; add `GET /index/{job_id}` for status and
   progress. The frontend polls.
2. **Parallelize the summary pass.** A `ThreadPoolExecutor` at 8–16 workers over
   `summarize_symbol` cuts a 50-minute pass to 3–6 minutes. Bound the concurrency
   to stay under provider rate limits.
3. **Add retry with backoff** in `llm.complete` for 429 and 5xx. Right now a rate
   limit is caught at [summarize.py:36-38](api/summarize.py#L36-L38) and silently
   degrades that symbol's summary to its bare name — permanently damaging the
   full-text index that the entire vectorless retrieval strategy depends on
   (CLAUDE.md §11), with nothing surfaced to the operator.

### 2.7 The project is not under version control

`git init` has not been run. No history, no branches, no review, no CI, no
rollback, no blame. `.gitignore` is already correct (`.env`, `venv/`, `repos/*`),
so this is a five-minute fix — but until it is done, there is no way to ship
safely, and the `.env` file sitting in the working directory with live API keys has
no protection beyond an untracked ignore rule.

**Fix:** `git init`, verify `git status` shows `.env` ignored, make the first commit.

---

## 3. P1 — Required for a real deployment

### 3.1 The graph cannot hold more than one repo

Symbol ids are module-derived only — `auth.AuthService.login`
([parsing.py:184-186](api/parsing.py#L184-L186)). Two indexed repos that both have
an `auth.py` collide on the `symbol_id` uniqueness constraint and silently merge
into one nonsensical graph. That is why `index_repo` wipes the database first
(§2.2): full-wipe is the workaround for having no isolation.

**Fix:** add a `repo` property to `:Symbol` and `:Community`, prefix ids
(`<repo>:auth.AuthService.login`), filter every tool query in
[tools.py](api/tools.py) by repo, and pass the active repo through `/ask` and
`/graph`. This unlocks incremental re-index, multi-repo hosting, and non-destructive
updates in one change. Do it before the id space is baked into anything else.

### 3.2 Cited-path highlighting over-lights via substring matching

```python
# api/agent.py:156
cited = sorted(sid for sid in seen_ids if sid in answer)
```

Substring containment. An answer mentioning `auth.AuthService.login` also matches
the ids `auth` and `auth.AuthService`, so parent file and class nodes light up as
part of the traced path whether or not the model cited them. On a large graph this
turns a precise "lit path" — the product's core wedge — into a fuzzy blob.

**Fix:** match on word boundaries against the full id, e.g.
`re.search(rf"(?<![\w.]){re.escape(sid)}(?![\w.])", answer)`. Cheap fix, direct
improvement to the headline feature.

### 3.3 `GET /graph` is nondeterministic and drops edges

```cypher
MATCH (s:Symbol) ... LIMIT $limit          -- no ORDER BY
MATCH (a)-[r:CALLS|IMPORTS|INHERITS]->(b) LIMIT $limit * 3
```

`LIMIT` without `ORDER BY` returns an arbitrary subset — the same repo can render a
different graph on each page load. The two queries are independently limited, so
edges routinely reference nodes outside the node set; the frontend filters them out
([App.jsx:27-28](web/src/App.jsx#L27-L28)), meaning real relationships silently
vanish from the visualization.

**Fix:** order nodes by degree descending (highest-signal first) and derive the edge
query from the selected node set so the two are consistent. Consider returning a
truncation flag so the UI can say "showing top 1,500 of N symbols".

### 3.4 Full source is stored on every node, in memory and in Neo4j

`Symbol.source` holds the complete text of every function, class, **and file**
([parsing.py:41](api/parsing.py#L41)). `parse_repo` accumulates all of it in memory
before writing, and `write_symbols` persists it via `SET s += row`. Because a file
symbol's source is the whole file and its members' sources are substrings of that,
the repo is stored roughly twice over.

**Fix:** store `start_line`/`end_line` (already captured) and read source from disk
on demand in `get_definition`, or at minimum stop storing `source` on `kind="file"`
nodes. Stream files through the pipeline rather than buffering all symbols.

### 3.5 No structured logging, metrics, or cost tracking

Everything is `print()` — [indexer.py:135](api/indexer.py#L135),
[parsing.py:313](api/parsing.py#L313), [summarize.py:37](api/summarize.py#L37).
There is no request id, no latency measurement, no error rate, and — most
importantly for a product whose unit economics are LLM calls — **no token or cost
accounting**. `/ask` can silently escalate to Opus at the step cap
([agent.py:144-150](api/agent.py#L144-L150)) with nothing recorded.

**Fix:** the `logging` module with JSON output; a request-id middleware; counters for
index duration, symbols indexed, summary failures, tool calls per question, steps
per question, and per-provider token usage. Log the token counts returned on every
LLM response in `llm.py` — that is the single highest-value instrument here.

### 3.6 Unpinned dependencies on both sides

`api/requirements.txt` pins only `tree-sitter`/`tree-sitter-languages` (correctly,
and for a documented reason). Everything else — `fastapi`, `neo4j`, `anthropic`,
`openai`, `leidenalg` — floats, so two builds of the same commit can install
different code. `web/Dockerfile:5` runs `npm install` with **no `package-lock.json`
in the repo**, so the frontend build is non-reproducible too.

**Fix:** `pip-compile` a fully-pinned `requirements.txt` with hashes; commit
`package-lock.json` and switch the Dockerfile to `npm ci`.

### 3.7 Error handling gaps on the API surface

- `POST /ask` has no try/except ([main.py:55-59](api/main.py#L55-L59)). A Neo4j
  outage or LLM error returns a 500 with a stack trace to the client.
- `POST /index` catches bare `Exception` and puts `str(exc)` in the response detail
  ([main.py:51-52](api/main.py#L51-L52)) — that can leak filesystem paths and
  internal state. Log the detail; return a generic message plus a correlation id.
- `@app.on_event("shutdown")` ([main.py:37](api/main.py#L37)) is deprecated; move to
  the `lifespan` context manager.
- No timeout on the agent loop. Eight Sonnet turns plus an Opus synthesis can exceed
  nginx's 300s `proxy_read_timeout` on a hard question.
- `find_symbol(limit=...)` and the other tools coerce an LLM-supplied `limit` with
  `int()` but never bound it ([tools.py:36](api/tools.py#L36)). Clamp to a maximum.

### 3.8 Prompt injection from indexed source is unmitigated

CLAUDE.md §11 correctly identifies tool results as untrusted, and the loop correctly
keeps them inside `tool_result` blocks. But indexed source is attacker-controlled in
the consultancy use case ("we inherited this client repo"), and it reaches the model
twice: at index time inside a summarization prompt
([summarize.py:24](api/summarize.py#L24), fenced but not delimited defensively) and
at query time via `get_definition`. The blast radius is bounded — every tool is
read-only, and the agent has no write access — but a malicious repo can steer answer
content and poison summaries, which are the retrieval index.

**Fix:** document the threat model explicitly; wrap untrusted source in the summary
prompt with clear delimiters and an instruction that its contents are data, not
instructions; consider a marker on summaries whose source contained
instruction-like patterns. Keep every tool read-only — that invariant is what makes
this containable.

### 3.9 Test coverage stops at the pure-Python layer

8 tests, all passing, covering `parsing`, `community`, and the agent protocol — the
right places to start, and the agent tests genuinely verify the load-bearing
tool-use ordering. Untested: `tools.py`, `graph.py`, `llm.py`, `main.py`,
`indexer.py`. CLAUDE.md §13 calls for integration tests that index a fixture repo
and assert node/edge counts and `find_symbol` results; those do not exist, so no
Cypher in the codebase has ever been executed by a test.

**Fix:** a `testcontainers`-backed Neo4j fixture, an integration test that indexes
`tests/fixtures/sample_repo` with `llm.py` mocked and asserts exact node/edge counts
plus `find_symbol` hits, and unit coverage for the OpenAI↔Anthropic translation in
`_to_openai_messages` (100 lines of subtle format conversion with zero tests today).

### 3.10 CI does not exist

No `.github/`, no pipeline. `ruff check` currently fails on two trivial findings
(`F541` at [indexer.py:135](api/indexer.py#L135), `F401` at
[parsing.py:24](api/parsing.py#L24)) — both `--fix`-able, and both evidence that
nothing gates merges today.

**Fix:** a workflow running `ruff check`, `ruff format --check`, `pytest`, and both
Docker builds on every push.

---

## 4. P2 — Polish before customers see it

- **Answers render as plain text.** The system prompt explicitly asks for fenced
  code blocks for optimization suggestions
  ([agent.py:43-49](api/agent.py#L43-L49)), but `App.jsx:166` renders `m.text`
  directly — so every suggestion arrives as unformatted, unhighlighted text.
  Add a markdown renderer with syntax highlighting.
- **`window.alert` for node details** ([App.jsx:89](web/src/App.jsx#L89)). Replace
  with an inline detail panel; it is the natural home for source, signature, and
  jump-to-caller/callee.
- **No dagre layout.** CLAUDE.md §10 specifies `dagre` for top-down call-flow views;
  `cytoscape-dagre` is not in `package.json` and only `fcose` is registered.
- **No loading or error state** on the initial graph fetch — a failure is a
  `console.error` and a blank canvas ([App.jsx:93](web/src/App.jsx#L93)).
- **No request cancellation.** An in-flight `ask` is not aborted on unmount.
- **nginx serves no security headers, gzip, or cache-control** for hashed assets
  ([nginx.conf](web/nginx.conf)).
- **Symbols outside the CALLS graph get no community.** `detect_communities` builds
  its vertex set from call edges only ([community.py:34-36](api/community.py#L34-L36)),
  so isolated helpers, config modules, and dataclasses are invisible to
  `list_communities` — exactly the "how is this organized?" question it serves.
- **`.python-version` says 3.11.9**; the Dockerfile and CLAUDE.md §6 say 3.12. Local
  dev and CI run a different interpreter than production.
- **No `api/.dockerignore`**, so `venv/`, `__pycache__/`, and `.pytest_cache/` are
  sent to the build context and can land in the image.

---

## 5. Target production topology

```
                    ┌─────────────────────────────────────────┐
   browser ──TLS──▶ │ reverse proxy (TLS, auth, rate limit)   │
                    └────────────────┬────────────────────────┘
                                     │
                    ┌────────────────▼──────────┐
                    │ web  (nginx, static SPA)  │
                    └────────────────┬──────────┘
                                     │ /api →
                    ┌────────────────▼──────────┐      ┌──────────────────┐
                    │ api  (FastAPI, uvicorn)   │─────▶│ LLM API          │
                    │  · /ask   — online        │      │ (only egress)    │
                    │  · /index — enqueue only  │      └──────────────────┘
                    └────────┬─────────┬────────┘
                             │         │ enqueue
                    ┌────────▼───┐  ┌──▼─────────────────┐
                    │ neo4j      │◀─│ indexer worker     │
                    │ (internal, │  │ (parse, summarize, │
                    │  volume)   │  │  leiden)           │
                    └────────────┘  └────────────────────┘
```

Key changes from today: nothing but the proxy is published to the host; Neo4j is
internal-only; indexing is a separate worker process so a long index cannot block or
time out the request path; the LLM API remains the only egress, as CLAUDE.md §2.3
requires.

**Single-tenant on-prem** (the likely B2B shape) can stay on Docker Compose with a
prod override. **Multi-tenant hosting is out of scope** until §3.1 repo scoping
lands — there is currently no isolation boundary in the graph at all.

---

## 6. Operational notes

**Cost model.** Indexing costs one Haiku call per symbol plus one per community —
roughly 5,000 calls for a 5,000-symbol repo, paid again on every re-index because
`index_repo` always does a full wipe. Querying costs up to 8 Sonnet turns, plus one
Opus synthesis whenever the step cap is hit. Community summaries are the expensive
part and are **not currently cached** despite CLAUDE.md §11 calling for it. Add
per-request cost logging (§3.5) before letting anyone else drive the system.

**Backup.** State lives in the `neo4j-data` volume. It is fully derivable from the
source repos, so the recovery story is "re-index" — but that costs real money and
time. For anything above a few thousand symbols, schedule `neo4j-admin database
dump` and keep the artifacts.

**Scaling limits as built.** Everything is single-process and single-repo. In-memory
symbol accumulation (§3.4) sets the practical ceiling — expect trouble somewhere
around 50k symbols. `neo4j` heap is fixed at 1G
([docker-compose.yml:11](docker-compose.yml#L11)) with no page-cache setting.

**Key rotation.** API keys arrive as compose environment variables from `.env`
([docker-compose.yml:26-29](docker-compose.yml#L26-L29)), which means they are
visible in `docker inspect` and the process environment. Move to Docker secrets or
a mounted secrets file for any shared host.

---

## 7. Checklist

**P0 — blocks any shared deployment**
- [ ] `git init`; first commit; confirm `.env` is ignored
- [ ] Validate and confine `/index` paths to `INDEX_ROOT` (§2.1)
- [ ] Authentication on all routes; narrow CORS to the real origin (§2.4)
- [ ] Remove `change-me` defaults; fail fast on missing `NEO4J_PASSWORD` (§2.3)
- [ ] Unpublish Neo4j ports in the production compose file (§2.3)
- [ ] Single-flight guard around `index_repo`; stop the unauthenticated wipe (§2.2)
- [ ] `docker-compose.prod.yml`: no bind mount, no `--reload`, restart policy,
      healthchecks (§2.5)
- [ ] `/index` returns 202 + job id; pipeline moves to a background worker (§2.6)
- [ ] Parallelize the summary pass; add retry/backoff on LLM calls (§2.6)

**P1 — required for a real deployment**
- [ ] Repo scoping on `:Symbol` / `:Community`; non-destructive re-index (§3.1)
- [ ] Word-boundary citation matching (§3.2)
- [ ] Deterministic, internally-consistent `/graph` query (§3.3)
- [ ] Stop double-storing source; stream the parse (§3.4)
- [ ] Structured logging, request ids, token/cost metrics (§3.5)
- [ ] Pin all Python deps; commit `package-lock.json`; `npm ci` (§3.6)
- [ ] Error handling on `/ask`; stop leaking exception text; `lifespan`; clamp
      tool `limit` (§3.7)
- [ ] Document the prompt-injection threat model; harden the summary prompt (§3.8)
- [ ] Integration tests against a real Neo4j; cover the OpenAI translation (§3.9)
- [ ] CI: ruff + pytest + Docker builds (§3.10)

**P2 — polish**
- [ ] Markdown + syntax highlighting for answers
- [ ] Replace `window.alert` with a detail panel
- [ ] Add `cytoscape-dagre` and the call-flow view
- [ ] Loading and error states; abort in-flight requests
- [ ] nginx security headers, gzip, cache-control
- [ ] Give call-graph-isolated symbols a community
- [ ] Align `.python-version` to 3.12; add `api/.dockerignore`
- [ ] Cache community summaries between re-indexes

---

## 8. What not to "fix"

These look like problems and are not. They are load-bearing decisions from
CLAUDE.md §2 — changing them breaks the product thesis.

| Looks like | Actually |
|---|---|
| No vector DB / embeddings | Deliberate (§2.1). Retrieval is lexical-over-summaries + graph traversal. Do not add one. |
| Leiden in Python, not Neo4j GDS | Deliberate (§2.4). GDS Leiden is edition-dependent. |
| Cypher f-strings in `tools.py` / `indexer.py` | Safe: interpolated values come from validated enums (`_KINDS`, `_EDGE_TYPES`) only. All user input is parameterized. |
| Unresolved calls dropped, not guessed | Deliberate (§2.2). The graph is the source of truth; a guessed edge is worse than a missing one. |
| Sync `def` handlers instead of `async def` | Correct as written. The Neo4j driver and both LLM SDKs are blocking; `def` handlers run in a threadpool and do not stall the event loop. CLAUDE.md §10's "async handlers" line is the thing that should change, not this code. |
| `tree-sitter` pinned to 0.21.3 | Required. `tree-sitter-languages` only links the 0.21.x C API. |
