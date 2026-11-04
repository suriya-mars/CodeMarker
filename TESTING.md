# Running the application for testing

Every command here was run end to end against a fresh stack on 2026-10-30.
Operational detail lives in [RUNBOOK.md](RUNBOOK.md); design in [CLAUDE.md](CLAUDE.md).

All commands run from the project root unless a step says otherwise:

```bash
cd /home/suriya/suriya/CodeMarker
```

---

## 0. One-time setup

```bash
cp .env.example .env        # only if there is no .env yet
```

`.env` must contain:

| Setting | Requirement |
|---|---|
| `NEO4J_PASSWORD` | A real password. The api refuses to start on an empty value or `change-me`. |
| `LLM_PROVIDER` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | A real key for the provider above |
| `OPENAI_MODEL` | Required when `LLM_PROVIDER=openai`, e.g. `gpt-4o-mini` |

For local testing leave `AUTH_MODE` unset or `none` (no login). `GUARD_MODE`
defaults to `required`; the Docker image ships the guardrail model.

---

## 1. Build and start the database

```bash
docker compose build                     # first build is slow: the guardrail model is baked into the image
docker compose up -d neo4j
docker inspect -f '{{.State.Health.Status}}' codemarker-neo4j-1    # repeat until it prints: healthy
```

---

## 2. Add a repo and index it

The repo must be a directory under `./repos`. It must be a git repository for
impact analysis to work.

```bash
cp -r /path/to/your/project ./repos/myrepo
docker compose run --rm api python -m indexer /repos/myrepo --repo myrepo
```

For a large repo, run the stages separately to see the symbol count before
paying for summaries:

```bash
docker compose run --rm api python -m indexer /repos/myrepo --repo myrepo --stage parse       # free
docker compose run --rm api python -m indexer /repos/myrepo --repo myrepo --stage summarize   # LLM cost
docker compose run --rm api python -m indexer /repos/myrepo --repo myrepo --stage community   # small LLM cost
```

An interrupted run can be repeated with the same command; finished work is skipped.

---

## 3. Start the whole application

```bash
docker compose up -d
```

| Service | URL |
|---|---|
| Web UI (graph + chat) | http://localhost:3000 |
| API | http://localhost:8000 |
| Neo4j browser | http://localhost:7474 (user `neo4j`, password from `.env`) |

---

## 4. Try each feature

**Health**

```bash
curl -s localhost:8000/health
```

**Ask a question.** `repo` binds the agent's tools to that repo.

```bash
curl -s localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"how does login work?","repo":"myrepo"}' \
  | jq '{answer, cited_ids, usage, stopped}'
```

**Impact analysis.** Commit a change in `./repos/myrepo` first. The report is
computed against the commit the graph was indexed at.

```bash
curl -s localhost:8000/impact -H 'Content-Type: application/json' \
  -d '{"repo":"myrepo"}' | jq -r .markdown

# with the LLM's explanation of the report
curl -s localhost:8000/impact -H 'Content-Type: application/json' \
  -d '{"repo":"myrepo","explain":true}' | jq -r '.analysis.answer'

# saved reports
curl -s localhost:8000/impact | jq
curl -s localhost:8000/impact/<report_id>/ask -H 'Content-Type: application/json' \
  -d '{"question":"which functions break?"}' | jq -r .answer
```

From the command line instead, with an optional re-index afterwards:

```bash
docker compose run --rm api python -m impact /repos/myrepo --repo myrepo --ask
docker compose run --rm api python -m impact /repos/myrepo --repo myrepo --reindex
```

**Usage and cost**

```bash
curl -s localhost:8000/me/usage | jq '.total'        # your own usage
curl -s localhost:8000/usage    | jq '{calls, input_tokens, output_tokens, cost_usd}'   # this process
docker compose run --rm api python -m quota report                                   # all users, 30 days
docker compose run --rm api python -m quota report --days 7 --user local             # one user, by day
```

Cost is an estimate from the prices in `api/telemetry.py`; the provider's
console shows what you are billed.

**Per-user limits.** With `AUTH_MODE=none` every request is the user `local`.

```bash
docker compose run --rm api python -m quota set local 500000     # tokens per UTC day
docker compose run --rm api python -m quota set local 0          # unlimited
docker compose run --rm api python -m quota clear local          # back to DAILY_TOKEN_QUOTA
docker compose run --rm api python -m quota list
```

**Inspect the graph** in the Neo4j browser (http://localhost:7474):

```cypher
MATCH (s:Symbol) RETURN s.kind, count(*) ORDER BY count(*) DESC;
MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY count(*) DESC;
MATCH (s:Symbol) WHERE s.summary STARTS WITH '[flagged' RETURN s.id, s.summary;
```

---

## 5. Run the tests

Unit tests and the real-model guardrail tests, inside the stack:

```bash
docker compose run --rm api python -m pytest -q -p no:cacheprovider
```

Integration tests **wipe the database they run against**. Always use a
throwaway Neo4j, never the application's:

```bash
docker run -d --rm --name neo4j-test -p 7688:7687 \
  -e NEO4J_AUTH=neo4j/testpassword123 neo4j:5-community
# wait ~20 s for it to start, then:
cd api
NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=testpassword123 \
  ../venv/bin/python -m pytest -q
cd ..
docker rm -f neo4j-test
```

The local virtualenv has no ML guardrail stack, so tests there run with
`GUARD_MODE=off`; the real-model tests run in the Docker image.

Lint:

```bash
docker compose run --rm api ruff check .
```

---

## 6. MCP server for Claude Code (optional)

Runs on the host and connects to the application's Neo4j.

```bash
cd api
NEO4J_URI=bolt://localhost:7687 \
NEO4J_PASSWORD="$(grep '^NEO4J_PASSWORD=' ../.env | cut -d= -f2- | cut -d' ' -f1)" \
TOOLS_REPO=myrepo \
  ../venv/bin/python -m mcp_server
```

---

## 7. Stop

```bash
docker compose down        # keeps the indexed graph
docker compose down -v     # also DELETES the graph; a full re-index follows
```

---

## Testing without touching your data

To try everything on a disposable copy, give Compose a different project name.
It gets its own containers and its own database volume:

```bash
docker compose -p cmtest build
docker compose -p cmtest up -d neo4j
docker compose -p cmtest run --rm api python -m indexer /repos/myrepo --repo myrepo
docker compose -p cmtest up -d
# ... same curl commands as above ...
docker compose -p cmtest down -v      # removes the copy and its database
```

Stop the main stack first if it is running; both use the same ports.

---

## Expected results and known issues

| Check | Expected |
|---|---|
| Indexing | Symbol, edge and community counts printed; `failed: 0` |
| `/ask` | An answer, `cited_ids`, `usage` with `cost_usd`, `stopped: null` |
| `/impact` after a signature change | A `SIGNATURE_CHANGED_CALLERS_UNTOUCHED` flag listing the callers |
| `/me/usage` | Your questions, tokens and cost for today |
| Web UI | The graph renders; asking a question highlights the cited symbols |

Known issues seen in testing:

- **Weak citations with `gpt-4o-mini`.** It tends to cite file ids (`auth`)
  instead of exact symbol ids (`auth.AuthService.login`), so less of the path
  lights up. A stronger model cites more precisely.
- **Occasional false guardrail flags on very short functions.** About 1 in 100
  functions, more often on two-line ones. The summary gets a `[flagged: …]`
  prefix; the symbol is still indexed and searchable.
- **4 integration tests fail** (`test_ingestion`, `test_integration`): they
  expect 19 fixture symbols, but the fixture now has 25. This predates the
  current work.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NEO4J_PASSWORD is unset or still the 'change-me' placeholder` | Set a real password in `.env` |
| `set NEO4J_PASSWORD in .env` from `docker compose` | Same: Compose refuses to start without it |
| `GUARD_MODE=required but llm-guard is not installed` | Use the Docker image, or set `GUARD_MODE=off` for local runs outside Docker |
| `repo 'myrepo' is not indexed` from `/impact` | Run step 2 first |
| `no repo named 'myrepo'` | The directory must be directly under `./repos` |
| `429 rate limit exceeded` | More than `LLM_RATE_PER_MIN` LLM calls in a minute (default 20) |
| `429 daily token quota exhausted` | Raise it: `python -m quota set local 0` |
| Port already in use | `docker compose down`, and stop any `-p cmtest` stack |

More in [RUNBOOK.md §7](RUNBOOK.md).
