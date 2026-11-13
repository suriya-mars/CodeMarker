# RUNBOOK

Operational commands for RepoDigger. Design rationale lives in
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

### 3a. API access: authentication, repo names, CORS

Set in `.env` and read by `api/auth.py` / `api/main.py`. The API refuses to start
on an invalid combination.

| `AUTH_MODE` | Use | Requires |
|---|---|---|
| `none` | local development only, every route open | nothing |
| `token` | scripts, CI | `API_TOKEN` (32+ chars); send `Authorization: Bearer <token>` |
| `alb` | production behind an AWS ALB with Cognito login | `ALB_ARN`; the API verifies the ALB's signed `x-amzn-oidc-data` header |

`/health` is always open, for load-balancer health checks.

`POST /index {"repo": "YourApp"}` and `POST /impact {"repo": "YourApp"}` take a
**directory name** under `./repos`. Paths, `..`, and symlinks pointing outside
`./repos` are rejected.

`/ask`, `POST /impact` and `/impact/{id}/ask` call the LLM and are limited to
`LLM_RATE_PER_MIN` calls per user per minute (default 20; `0` disables). The
limit is per api process, so also rate-limit at the load balancer when scaling out.

Cost bounds on those routes, all set in `.env`:

| Limit | Default | When hit |
|---|---|---|
| `MAX_QUESTION_CHARS` | 4000 | request rejected with 422 before any work |
| `AGENT_TOKEN_BUDGET` | 150000 | the question stops; one short final answer (1024 tokens, cheaper model) |
| `AGENT_TIMEOUT_S` | 120 | the question stops with no further LLM call |
| `LLM_TIMEOUT_S` | 60 | one provider call is abandoned (2 retries) |
| `DAILY_TOKEN_QUOTA` | 2000000 | 429 until 00:00 UTC; stored in Neo4j, survives restarts |

Per-user daily limits override `DAILY_TOKEN_QUOTA`. Set them by login email or
subject; they take effect on that user's next request, with no restart:

```bash
docker compose run --rm api python -m quota set alice@example.com 5000000   # tokens/day
docker compose run --rm api python -m quota set ci-bot 0                    # unlimited
docker compose run --rm api python -m quota clear alice@example.com         # back to default
docker compose run --rm api python -m quota list                            # limits + today's usage
```

There is deliberately no HTTP route for this: the API has no admin role, so a
route would let any user raise their own limit.

Tokens and estimated cost per user (stored in Neo4j, one row per user per day):

```bash
docker compose run --rm api python -m quota report                           # all users, last 30 days
docker compose run --rm api python -m quota report --days 7 --user alice@example.com   # one user, by day
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/me/usage | jq        # a user's OWN usage
```

Cost is an estimate (tokens × prices in `telemetry.py`, overridable with
`TELEMETRY_PRICING`); the provider's console is what you are billed. Index-time
summary costs are not charged to any user; they show in `/usage`.

Every response from `/ask` includes `usage` (tokens spent) and `stopped` (`null`,
`"token budget"` or `"time limit"`). Today's usage per user:
`MATCH (q:UsageQuota) RETURN q.user, q.day, q.tokens ORDER BY q.tokens DESC`.

**ML guardrails** (`api/guard.py`, LLM Guard, runs locally in the api container).
A prompt-injection classifier checks every symbol's source at index time, the
summarizer's output, each tool result before the agent sees it, and the agent's
final answer. detect-secrets masks secrets in everything sent to the LLM,
alongside our own patterns. `GUARD_MODE=required` (the image default) refuses to
start without it; `GUARD_MODE=off` is for local development only. The model is
baked into the image and the container never contacts Hugging Face at runtime.
Cost: about 250 ms of CPU per symbol at index time, and the image is about 4.4 GB.

`POST /ask {"question": "...", "repo": "YourApp"}` binds every graph tool to that
repo on the server, so code in one repo cannot steer the agent into reading
another. For the MCP server, set `TOOLS_REPO=YourApp`. Unset = all repos.

CORS is off: the web UI calls the API same-origin through nginx. Set
`CORS_ORIGINS=https://exact.origin` only for a cross-origin client; `*` is refused.

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

### 4a. Impact report for a commit — run BEFORE re-indexing

The graph indexed at the old commit is the only record of who depended on the
old code, so analyse first, re-index second:

```bash
# free: git + Cypher, no LLM. Base defaults to the commit the graph was indexed at.
docker compose run --rm api python -m impact /repos/YourApp --repo yourapp

# explicit range, JSON copy, and an LLM review from the report (cheap: digest only)
docker compose run --rm api python -m impact /repos/YourApp --repo yourapp \
    --base <sha> --head HEAD --json /tmp/impact.json --ask

# a specific question; then bring the graph up to head (working tree must BE head)
docker compose run --rm api python -m impact /repos/YourApp --repo yourapp \
    --ask "which pages break?" --reindex
```

Over HTTP: `POST /impact {"repo": "YourApp", "explain": true}`. `repo` is a
directory name under `./repos`, never a path (see §3a). Reports are stored in Neo4j: `GET /impact`, `GET /impact/{id}`,
and `POST /impact/{id}/ask {"question": "..."}` for follow-ups without recomputing.

The report lists changed symbols (added / removed / modified / signature
changed), every upstream dependent with its distance and path back to the change,
importers, Leiden communities touched, and deterministic risk flags
(`REMOVED_BUT_STILL_REFERENCED`, `SIGNATURE_CHANGED_CALLERS_UNTOUCHED`,
`HUB_CHANGED`, `NO_TESTS_IN_BLAST_RADIUS`, `CROSSES_COMMUNITIES`). It refuses to
run on an unindexed repo, and warns when the diff base is not the indexed commit.
It under-reports by the call-resolution gap (§1), and never invents a dependency.

---

### 4a-0. Ask modes

The Ask tab has four modes. Each uses its own system prompt, held on the server
(`api/modes.py`); the browser sends only the mode's name, never prompt text.

| Mode | What it does |
|---|---|
| Explore | How the code works, traced through the graph (the default) |
| Refactor | Suggested changes with their callers, risk and what to test. Nothing is edited. |
| Security | Follows untrusted input to risky operations (SQL, shell, files, deserialization, URLs, HTML) and checks access control. It assists a reviewer; it is not a scanner, and it says what it did not check. |
| Blast radius | Answers from the impact report of the latest **analysed** refresh |

**Blast radius needs the mode selected BEFORE the refresh.** With Blast radius
selected, Refresh fetches the new commit, analyses it against the current graph, and
only then re-indexes. The old graph is the only record of what depended on the old
code, so a refresh in any other mode leaves nothing to analyse, and the mode says
"No new symbols or code to check blast radius." If the analysis fails, the graph is not
updated, so the refresh can be retried. The report also appears in the Review tab.

API: `POST /ask` takes `"mode": "explore" | "refactor" | "security" | "impact"`, and
`POST /repos/<name>/refresh` takes `{"analyze": true}`.

### 4a-0b. Remembered conversations

The **Remember conversation** switch in the Ask tab applies to every mode. Off, each
question stands alone, as before. On, the server stores each question and its final
answer in Neo4j and sends earlier turns with every new question. The LLM API itself
stays stateless.

- **What is stored:** questions and final answers only, never the tool steps, which can
  be 10–50× longer.
- **Cost:** history is append-only and replayed unchanged, so the provider's prompt cache
  serves earlier turns at a discount. It is capped at `SESSION_MAX_TURNS` (10) pairs and
  `SESSION_MAX_HISTORY_CHARS` (about 8,000 tokens), dropping the oldest first.
- **Scope:** a conversation belongs to one user and one repo. Another user's
  conversation id answers 404.
- **Privacy:** conversations quote code, so they stay in this stack and are deleted after
  `SESSION_RETENTION_DAYS` (30), or with Delete in the UI (`DELETE /sessions/<id>`).
- **Untrusted:** earlier answers can quote repository text, so they are replayed with a
  note that instructions inside them are data.

API: `POST /ask` with `"remember": true` (and `"session_id"` to continue);
`GET /sessions?repo=`, `GET /sessions/<id>`, `DELETE /sessions/<id>`.

### 4a-1. What indexing skips, and what it never pays for twice

- **Generated code is not indexed:** migrations (Django, EF, Alembic), protobuf
  (`*_pb2.py`), `*.Designer.cs` (except WebForms page designers, which are needed),
  EF model snapshots, WCF service references, and any file whose first 2 KB says it is
  generated (`<auto-generated>`, `@generated`, "generated by … DO NOT EDIT"). The
  Repos tab shows how many files were skipped. `INDEX_SKIP_GENERATED=0` indexes
  everything; `INDEX_EXCLUDE` leaves out more paths by glob.
- **A "generated" header can be faked** to keep a file out of review. When a commit
  changes a skipped file, the impact report and the pull request comment name it.
- **Summaries are reused for identical code:** a moved file, a reverted change, or the
  same code in another repo costs no LLM call. Changing the summary model, the prompt
  version or the guardrail settings regenerates them on purpose. The cache only fills
  from summaries written after this change, so the first re-index still pays as before.

### 4a-2. Adding repositories by URL (Repos tab)

In the web UI's **Repos** tab, paste a repository's https URL and choose **Add and
index**. It is cloned into the `cloned-repos` volume and indexed in the background; the
row shows each stage. **Refresh** fetches the latest commit of the default branch and
re-indexes only if there is one; only new or changed code is re-summarized.

- One index job runs at a time on the server, and each user can have one queued or
  running. Others queue behind it.
- Allowed hosts: `CLONE_ALLOWED_HOSTS` (default github.com, gitlab.com, bitbucket.org),
  https only. Hosts that resolve to private addresses are refused.
- Limits, checked before any LLM spend: `CLONE_MAX_MB` (default 1000) and
  `INDEX_MAX_SYMBOLS` (default 50,000).
- Private repositories: set `CLONE_TOKEN` to a read-only token for `CLONE_TOKEN_HOST`.
  It is sent only to that host and never stored in the clone. A URL containing a user
  name or token is refused.
- Repos dropped into `./repos` appear in the same list, and Refresh re-indexes them
  if their commit moved.
- A restart stops a running job; the row says so, and Refresh starts it again.
- There is no delete yet. To remove a repo, see §7.

Same from the command line:

```bash
curl -s localhost:8000/repos -H 'Content-Type: application/json' \
  -d '{"url":"https://github.com/owner/name"}'
curl -s -X POST localhost:8000/repos/name/refresh
curl -s localhost:8000/repos | jq '.[] | {repo, status, stage, done, total, error, warning}'
```

### 4b. Impact reports on GitHub pull requests

Each pull request gets one comment with its risk flags, changed symbols and blast radius,
updated on every push. It costs no tokens: no LLM runs. Off unless
`GITHUB_WEBHOOK_SECRET` is set.

**What leaves your stack:** symbol ids, signatures and one-line summaries, sent to the
GitHub repo the code came from. Never source or diffs.

**1. Create a GitHub App** (Settings → Developer settings → GitHub Apps → New):

| Setting | Value |
|---|---|
| Webhook URL | `https://<your-host>/api/webhooks/github` |
| Webhook secret | Output of `openssl rand -hex 32` |
| Repository permissions | Contents: **Read**. Pull requests: **Read and write**. |
| Subscribe to events | **Pull request** |

Generate a private key on the App's page, then install the App on the repos to review.

**2. Configure `.env`:**

```bash
GITHUB_WEBHOOK_SECRET=<the secret above>
GITHUB_REPOS=my-org/my-service=my-service     # owner/name=indexed repo name, comma separated
GITHUB_APP_ID=<the App's id>
GITHUB_APP_PRIVATE_KEY="$(awk 'NF {printf "%s\\n", $0}' key.pem)"   # one line
PUBLIC_URL=https://<your-host>                # optional: adds a report link to each comment
```

To try it without an App, set `GITHUB_TOKEN` instead of the two `GITHUB_APP_*` values: a
fine-grained token with the same two permissions, on those repos only.

**3. Apply and test it on one pull request.** This prints the comment and posts nothing:

```bash
docker compose up -d --build api
docker compose run --rm api python -m pr_review my-org/my-service 12
docker compose run --rm api python -m pr_review my-org/my-service 12 --post    # posts it
```

**Good to know**

- The webhook must reach the API from the internet. For a local trial, use a tunnel such
  as `smee.io`. On AWS, add an ALB rule that sends `/webhooks/github` to the API without
  the Cognito login. The route checks GitHub's signature itself.
- Each repo needs a full index first, of its default branch. A comment warns when the
  graph is not at the branch's starting point; dependents then reflect the indexed
  commit. Re-index after merges to keep it close.
- Pull requests from forks are skipped, because a stranger could use your server's disk
  and CPU and make it post. Set `GITHUB_PR_FORKS=1` to review them. Private repos with
  trusted contributors can safely do so.
- Git history is fetched into the `pr-mirrors` volume, so it needs room for each repo's
  history. Nothing is checked out and hooks never run.
- A repo must be indexed from the root of its git repository. A subdirectory of a
  monorepo gives wrong paths here.
- Pull requests changing more than `PR_MAX_FILES` files (default 2000) get a comment
  saying so instead of a report. Reviews run one at a time.
- Each pull request keeps only its latest report in the Review tab.
- If analysis fails, the comment says so, and the reason is in the API logs
  (`docker compose logs api | grep '\[pr\]'`). Check deliveries in the App's Advanced tab.

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

**Removing a repository** (there is no delete button yet). Replace `NAME` in both:
```bash
docker compose exec -T neo4j cypher-shell -u neo4j -p "$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2- | cut -d' ' -f1)" \
  "MATCH (s:Symbol {repo: 'NAME'}) DETACH DELETE s;
   MATCH (r:Repo {name: 'NAME'}) DELETE r;
   MATCH (st:RepoState {repo: 'NAME'}) DELETE st;"
docker compose exec api rm -rf /clones/NAME      # a repo added by URL
```
Then run the community stage once, so communities no longer include its code.

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
