# RUNBOOK

Operational commands for Codebase Explorer. Design rationale lives in
[CLAUDE.md](CLAUDE.md); readiness gaps in [PRODUCTION.md](PRODUCTION.md); the
.NET/WebForms plan in [PRODUCTION-DOTNET.md](PRODUCTION-DOTNET.md).

Every command here has been run. Where something is documented but unverified it
says so.

---

## 0. Setup

```bash
cd /home/suriya/suriya/CodeMarker
cp .env.example .env      # first time only
```

`.env` must have a real key for your chosen provider:

```bash
LLM_PROVIDER=openai                 # or: anthropic
OPENAI_API_KEY=sk-...               # if provider=openai
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=sk-ant-...        # if provider=anthropic
NEO4J_PASSWORD=<something-real>     # NEVER leave this unset
```

Check what is actually configured (values hidden):

```bash
# flags empty values AND unedited placeholders (sk-ant-..., change-me, sk-...)
awk -F= '/=/ && !/^#/ {v=$2; gsub(/^[ \t]+|[ \t]+$/,"",v);
  bad = (v=="" || v ~ /^sk-ant-\.\.\.$|^sk-\.\.\.$|change-me/);
  printf "  %-20s %s\n", $1, (bad ? "!! NOT SET / placeholder" : "ok")}' .env

grep -E '^(LLM_PROVIDER|OPENAI_MODEL)=' .env
```

Only the key for your active `LLM_PROVIDER` needs to be real; the other may stay
a placeholder.

Optional tuning, both read by `docker-compose.yml`:

```bash
NEO4J_HEAP=4G           # default 4G; lower on a small machine
NEO4J_PAGECACHE=2G      # default 2G
```

---

## 1. Measure first  ← always start here

Free. No LLM, no database. Tells you whether the graph will be trustworthy
before you spend anything.

```bash
# from the project root — takes any host path
venv/bin/python api/measure.py /path/to/YourApp
```

**Read the CALL RESOLUTION number.**

| Result | Action |
|---|---|
| **>= 60%** | Proceed to indexing |
| **< 60%** | Stop. Blast radius will under-report, so `get_page_brief` will miss dependencies. Escalate to Roslyn (PRODUCTION-DOTNET.md §8.5) |

Also check: parse failures should be near zero, and the dropped-names list should
be mostly framework calls (`ToString`, `SqlCommand`) rather than your own methods.

---

## 2. Index

```bash
docker compose up -d neo4j
docker compose ps                 # wait for "healthy"

cp -r /path/to/YourApp ./repos/
```

Run the stages separately the first time, so you see the symbol count before
paying for summaries:

```bash
# FREE — parsing only
docker compose run --rm api python -m indexer /repos/YourApp \
    --repo yourapp --stage parse

# COSTS MONEY — one LLM call per symbol
docker compose run --rm api python -m indexer /repos/YourApp \
    --repo yourapp --stage summarize

# cheap — Leiden clustering + one summary per community
docker compose run --rm api python -m indexer /repos/YourApp \
    --repo yourapp --stage community
```

Or all three: drop the `--stage` flag.

**Interrupted? Run the same command again.** Every stage is idempotent; the
summariser reads its work queue from the graph, so completed work is skipped.

```
--repo NAME     scope for this codebase (default: the directory name).
                Required if more than one repo shares the database.
--workers N     summariser concurrency (default: from the scale profile)
--fresh         WIPE the graph first. Destructive, opt-in, rarely what you want.
```

---

## 3. Verify

```bash
docker compose up -d              # web :3000, api :8000, neo4j browser :7474

curl -s localhost:8000/health
curl -s localhost:8000/usage | jq            # tokens + cost so far
curl -s localhost:8000/graph | jq '.nodes | length'
```

Ask it something you already know the answer to:

```bash
curl -s localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"give me the migration brief for OrderList.aspx"}' | jq -r .answer
```

Sanity-check the graph directly in the Neo4j browser (http://localhost:7474):

```cypher
MATCH (s:Symbol) RETURN s.kind, count(*) ORDER BY count(*) DESC;
MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY count(*) DESC;
MATCH (s:Symbol) WHERE s.summary IS NULL RETURN count(s);          -- should be 0
MATCH (p:Symbol {kind:'page'})-[:HANDLES]->(m) RETURN p.name, count(m) LIMIT 10;
```

**Judge it here.** If the page brief matches what you already know about that
page, the graph is trustworthy. If it misses things you know exist, that tells
you more than any metric.

---

## 4. Re-index when someone pushes

```bash
docker compose run --rm api python -m indexer /repos/YourApp --repo yourapp
```

Cheap by design:

* unchanged code keeps its summary — **no LLM cost**
* edited code is re-summarised (its `source` changed)
* deleted code is removed from the graph, scoped to `--repo`

**Trigger it on merge to main, in CI.** Not on local edits, not per commit.
A stale graph guiding a refactor is worse than no graph — someone must own this.

---

## 5. MCP server (read-only, for Claude Code)

Each engineer runs this locally; it connects to ONE shared Neo4j.

```bash
cd api
NEO4J_URI=bolt://localhost:7687 \
NEO4J_PASSWORD="$(grep '^NEO4J_PASSWORD=' ../.env | cut -d= -f2-)" \
  ../venv/bin/python -m mcp_server
```

Exposes 9 read-only tools. It refuses to start if any registered tool can write,
and fails closed if a tool cannot be verified. Indexing is deliberately NOT an
MCP tool, so teammates cannot trigger it or reach a path parameter.

Preflight status is printed to stderr on startup (stdout is the MCP transport —
never print to it).

---

## 6. Development

```bash
cd api
../venv/bin/python -m pytest -q          # integration tests skip without Neo4j
../venv/bin/ruff check .
../venv/bin/ruff format .
```

Run the integration tests against a throwaway database:

```bash
docker run -d --name neo4j-test -p 7688:7687 \
  -e NEO4J_AUTH=neo4j/testpassword123 neo4j:5-community

NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=testpassword123 \
  ../venv/bin/python -m pytest -q

docker rm -f neo4j-test
```

Run the suite inside the built image (tests what actually ships, since compose
bind-mounts `./api` over `/app`):

```bash
docker compose build api
docker run --rm codemarker-api python -m pytest -q
```

---

## 7. Troubleshooting

Every entry below is an error hit in practice.

**`No module named measure`**
`python -m X` needs `X.py` in the current directory. Run it by path instead —
that works from anywhere:
```bash
venv/bin/python api/measure.py /path/to/App        # not: python -m measure
```

**`path not found inside this process: /home/...`**
You passed a host path to a command running in the container. The container sees
only `/repos` (mounted from `./repos`) and `/app`:
```bash
cp -r /path/to/App ./repos/
docker compose run --rm --no-deps api python -m measure /repos/App
```

**`0 parseable files, 0 LOC`**
Same cause as above, or the directory has no supported extensions
(`.py .cs .aspx .ascx .master`). `bin/ obj/ node_modules/ .git/` and generated
`*.g.cs` are skipped deliberately.

**Neo4j exits immediately, `Unrecognized setting`**
Neo4j maps env vars `_` -> `.` and `__` -> `_`. `server.memory.pagecache.size`
is `NEO4J_server_memory_pagecache_size`; `server.memory.heap.max_size` is
`NEO4J_server_memory_heap_max__size`. Check with:
```bash
docker compose logs neo4j | tail -20
```

**Neo4j will not start / host is low on RAM**
Lower the limits in `.env`: `NEO4J_HEAP=1G`, `NEO4J_PAGECACHE=512m`.

**Every tool returns empty results**
The graph is empty or you are pointed at the wrong database:
```bash
docker compose run --rm api python -c "import graph; print(graph._run('MATCH (s:Symbol) RETURN count(s) AS n'))"
```

**Summaries never complete / many failures**
Rerun `--stage summarize` — failures stay pending and are retried. Persistent
failures usually mean a bad API key or rate limiting; the failure line names the
symbol and the provider error.

**`/usage` shows a placeholder-cost warning**
Your model has no published rate in `telemetry.py`. Add one without touching code:
```bash
TELEMETRY_PRICING='{"your-model": [0.15, 0.60]}'
```

**Port already in use**
```bash
docker compose down          # keeps the neo4j-data volume
docker compose ps -a
```

---

## 8. Cost reference

Per full index of ~125,000 symbols (~600 in / 40 out per symbol):

| model | cost |
|---|---|
| gpt-4o-mini | ~$14 |
| claude-haiku-4-5 | ~$100 |
| gpt-4o | ~$238 |

Re-indexing after a merge costs only the changed symbols — typically cents.
Live totals: `curl -s localhost:8000/usage | jq`.

---

## 9. Shutdown

```bash
docker compose down                  # keeps the neo4j-data volume
docker compose down -v               # DELETES the index; a full re-index follows
docker volume ls | grep neo4j
```
